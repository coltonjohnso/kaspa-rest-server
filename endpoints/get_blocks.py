# encoding: utf-8
import logging
import os
from asyncio import wait_for
from typing import List

from fastapi import Query, Path, HTTPException
from fastapi import Response
from pydantic import BaseModel
from sqlalchemy import select, exists, func

from constants import BPS
from dbsession import async_session, async_session_blocks
from endpoints.get_virtual_chain_blue_score import current_blue_score_data
from helper.difficulty_calculation import bits_to_difficulty
from helper.mining_address import get_miner_payload_from_block, retrieve_miner_info_from_payload
from helper.utils import add_cache_control
from kaspad.KaspadRpcClient import kaspad_rpc_client
from models.Block import Block
from models.BlockParent import BlockParent
from models.BlockTransaction import BlockTransaction
from models.Subnetwork import Subnetwork
from models.Transaction import TransactionOutput, TransactionInput, Transaction
from models.TransactionAcceptance import TransactionAcceptance
from server import app, kaspad_client

_logger = logging.getLogger(__name__)

IS_SQL_DB_CONFIGURED = os.getenv("SQL_URI") is not None


class VerboseDataModel(BaseModel):
    hash: str = "18c7afdf8f447ca06adb8b4946dc45f5feb1188c7d177da6094dfbc760eca699"
    difficulty: float | None = (4102204523252.94,)
    selectedParentHash: str | None = "580f65c8da9d436480817f6bd7c13eecd9223b37f0d34ae42fb17e1e9fda397e"
    transactionIds: List[str] | None = ["533f8314bf772259fe517f53507a79ebe61c8c6a11748d93a0835551233b3311"]
    blueScore: str | None = "18483232"
    childrenHashes: List[str] | None = None
    mergeSetBluesHashes: List[str] | None = []
    mergeSetRedsHashes: List[str] | None = []
    isChainBlock: bool | None = False


class ParentHashModel(BaseModel):
    parentHashes: List[str] = ["580f65c8da9d436480817f6bd7c13eecd9223b37f0d34ae42fb17e1e9fda397e"]


class BlockHeader(BaseModel):
    version: int | None = 1
    hashMerkleRoot: str | None = "e6641454e16cff4f232b899564eeaa6e480b66069d87bee6a2b2476e63fcd887"
    acceptedIdMerkleRoot: str | None = "9bab45b027a0b2b47135b6f6f866e5e4040fc1fdf2fe56eb0c90a603ce86092b"
    utxoCommitment: str | None = "236d5f9ffd19b317a97693322c3e2ae11a44b5df803d71f1ccf6c2393bc6143c"
    timestamp: str | None = "1656450648874"
    bits: int | None = 455233226
    nonce: str | None = "14797571275553019490"
    daaScore: str | None = "19984482"
    blueWork: str | None = "2d1b3f04f8a0dcd31"
    parents: List[ParentHashModel] | None
    blueScore: str | None = "18483232"
    pruningPoint: str | None = "5d32a9403273a34b6551b84340a1459ddde2ae6ba59a47987a6374340ba41d5d"


class BlockTxInputPreviousOutpointModel(BaseModel):
    transactionId: str
    index: int


class BlockTxInputModel(BaseModel):
    previousOutpoint: BlockTxInputPreviousOutpointModel | None
    signatureScript: str | None
    sigOpCount: int | None
    sequence: int | None


class BlockTxOutputScriptPublicKeyModel(BaseModel):
    scriptPublicKey: str | None
    version: int | None


class BlockTxOutputVerboseDataModel(BaseModel):
    scriptPublicKeyType: str | None
    scriptPublicKeyAddress: str | None


class BlockTxOutputModel(BaseModel):
    amount: int | None
    scriptPublicKey: BlockTxOutputScriptPublicKeyModel | None
    verboseData: BlockTxOutputVerboseDataModel | None


class BlockTxVerboseDataModel(BaseModel):
    transactionId: str
    hash: str | None
    computeMass: int | None
    blockHash: str | None
    blockTime: int | None


class BlockTxModel(BaseModel):
    inputs: List[BlockTxInputModel] | None
    outputs: List[BlockTxOutputModel] | None
    subnetworkId: str | None
    payload: str | None
    verboseData: BlockTxVerboseDataModel
    lockTime: int | None
    gas: int | None
    mass: int | None
    version: int | None


class ExtraModel(BaseModel):
    color: str | None = None
    minerAddress: str | None = None
    minerInfo: str = None


class BlockModel(BaseModel):
    header: BlockHeader
    transactions: List[BlockTxModel] | None
    verboseData: VerboseDataModel
    extra: ExtraModel | None


class BlockResponse(BaseModel):
    blockHashes: List[str] = [
        "44edf9bfd32aa154bfad64485882f184372b64bd60565ba121b42fc3cb1238f3",
        "18c7afdf8f447ca06adb8b4946dc45f5feb1188c7d177da6094dfbc760eca699",
        "9a822351cd293a653f6721afec1646bd1690da7124b5fbe87001711406010604",
        "2fda0dad4ec879b4ad02ebb68c757955cab305558998129a7de111ab852e7dcb",
    ]
    blocks: List[BlockModel] | None


@app.get("/blocks/{blockId}", response_model=BlockModel, tags=["Kaspa blocks"])
async def get_block(
    response: Response,
    blockId: str = Path(regex="[a-f0-9]{64}"),
    includeTransactions: bool = True,
    includeColor: bool = False,
):
    """
    Get block information for a given block id
    """
    block = await get_block_from_kaspad(blockId, includeTransactions, includeColor)
    if not block and IS_SQL_DB_CONFIGURED:
        response.headers["X-Data-Source"] = "Database"
        block = await get_block_from_db(blockId, includeTransactions)
        if block:
            logging.debug(f"Found block {blockId} in database")
            if includeColor:
                if block["verboseData"]["isChainBlock"]:
                    block["extra"] = {"color": "blue"}
                else:
                    block["extra"] = {"color": await get_block_color_from_db(block)}
    if block:
        miner_payload = get_miner_payload_from_block(block)
        if miner_payload:
            miner_info, miner_address = retrieve_miner_info_from_payload(miner_payload)
            block.setdefault("extra", {})
            block["extra"]["minerInfo"] = miner_info
            block["extra"]["minerAddress"] = miner_address
        if not includeTransactions:
            block["transactions"] = None
    else:
        raise HTTPException(status_code=404, detail="Block not found", headers={"Cache-Control": "public, max-age=8"})

    add_cache_control(block.get("header", {}).get("blueScore"), block.get("header", {}).get("timestamp"), response)
    return block


@app.get("/blocks", response_model=BlockResponse, tags=["Kaspa blocks"])
async def get_blocks(
    response: Response,
    lowHash: str = Query(regex="[a-f0-9]{64}"),
    includeBlocks: bool = False,
    includeTransactions: bool = False,
):
    """
    Lists block beginning from a low hash (block id).
    """
    response.headers["Cache-Control"] = "public, max-age=3"

    rpc_client = await kaspad_rpc_client()
    request = {"lowHash": lowHash, "includeBlocks": includeBlocks, "includeTransactions": includeTransactions}
    if rpc_client:
        try:
            resp = await wait_for(rpc_client.get_blocks(request), 60)
            for block in resp.get("blocks", []):
                convert_to_legacy_block(block)
            return resp
        except Exception:
            return {"blockHashes": [], "blocks": []}
    else:
        resp = await kaspad_client.request("getBlocksRequest", request)
        return resp["getBlocksResponse"]


@app.get("/blocks-from-bluescore", response_model=List[BlockModel], tags=["Kaspa blocks"])
async def get_blocks_from_bluescore(response: Response, blueScore: int = 43679173, includeTransactions: bool = False):
    """
    Lists blocks of a given blueScore
    """
    response.headers["X-Data-Source"] = "Database"

    if blueScore < 0 or current_blue_score_data["blue_score"] and current_blue_score_data["blue_score"] - blueScore < 0:
        return []

    add_cache_control(blueScore, None, response)

    # If the blue score is not older than 1 day, try looking up hashes and finding the blocks in kaspad first
    if (current_blue_score_data["blue_score"] and current_blue_score_data["blue_score"] - blueScore) / BPS < 86400:
        async with async_session_blocks() as s:
            block_hashes = (await s.execute(select(Block.hash).where(Block.blue_score == blueScore))).scalars().all()

        if not block_hashes:
            return []

        result = []
        for block_hash in block_hashes:
            block = await get_block_from_kaspad(block_hash, includeTransactions, False)
            if block:
                result.append(block)
        if result:
            return result

    # Block hashes not found in kaspad, look up blocks in the db instead
    async with async_session_blocks() as s:
        blocks = (await s.execute(block_join_query().where(Block.blue_score == blueScore))).all()

    result = []
    for block, is_chain_block, parents, children, transaction_ids in blocks:
        transactions = None
        if includeTransactions and transaction_ids:
            transactions = await get_transactions(block.hash, transaction_ids)
        result.append(map_block_from_db(block, is_chain_block, parents, children, transaction_ids, transactions))

    return result


async def get_block_from_kaspad(block_hash, include_transactions, include_color):
    rpc_client = await kaspad_rpc_client()
    request = {"hash": block_hash, "includeTransactions": include_transactions}
    if rpc_client:
        try:
            resp = await wait_for(rpc_client.get_block(request), 10)
            block = convert_to_legacy_block(resp.get("block", {}))
            logging.debug(f"Found block in kaspad (wrpc): {block_hash}")
        except Exception:
            block = {}
    else:
        resp = await kaspad_client.request("getBlockRequest", request)
        block = resp.get("getBlockResponse", {}).get("block", {})
        logging.debug(f"Found block in kaspad (grpc): {block_hash}")
    if not block.get("verboseData", {}).get("isHeaderOnly", True):
        block["extra"] = {}
        if include_color:
            if block["verboseData"]["isChainBlock"]:
                block["extra"]["color"] = "blue"
            else:
                block["extra"]["color"] = await get_block_color_from_kaspad(block["verboseData"]["hash"])
        return block


async def get_block_from_db(block_hash, include_transactions):
    async with async_session_blocks() as s:
        result = (await s.execute(block_join_query().where(Block.hash == block_hash).limit(1))).first()

    if result:
        block, is_chain_block, parents, children, transaction_ids = result
    else:
        async with async_session() as s:
            result = (
                await s.execute(
                    select(
                        BlockTransaction.transaction_id.label("transaction_id"),
                        Transaction.block_time.label("block_time"),
                    )
                    .join(Transaction, BlockTransaction.transaction_id == Transaction.transaction_id)
                    .where(BlockTransaction.block_hash == block_hash)
                )
            ).all()
            if not result:
                return None
            block = Block(hash=block_hash, timestamp=result[0].block_time)
            is_chain_block = None
            parents = []
            children = []
            transaction_ids = [row.transaction_id for row in result]

    transactions = (
        await get_transactions(block.hash, transaction_ids) if include_transactions and transaction_ids else None
    )
    return map_block_from_db(block, is_chain_block, parents, children, transaction_ids, transactions)


async def get_block_color_from_kaspad(block_hash):
    rpc_client = await kaspad_rpc_client()
    request = {"hash": block_hash}
    if rpc_client:
        try:
            resp = await wait_for(rpc_client.get_current_block_color(request), 10)
        except Exception:
            resp = {}
    else:
        resp = await kaspad_client.request("getCurrentBlockColorRequest", request)
        resp = resp.get("getCurrentBlockColorResponse", {})
    if resp.get("blue"):
        return "blue" if resp["blue"] is True else "red"


async def get_block_color_from_db(block):
    blockId = block["verboseData"]["hash"]
    async with async_session_blocks() as s:
        blocks = (
            (
                await s.execute(
                    select(Block)
                    .distinct()
                    .join(TransactionAcceptance, TransactionAcceptance.block_hash == Block.hash)
                    .join(BlockParent, BlockParent.block_hash == TransactionAcceptance.block_hash)
                    .filter(BlockParent.parent_hash == blockId)
                )
            )
            .scalars()
            .all()
        )
        for block in blocks:
            if blockId in block.merge_set_blues_hashes:
                return "blue"
            elif blockId in block.merge_set_reds_hashes:
                return "red"
    return None


def map_block_from_db(block, is_chain_block, parents, children, transaction_ids, transactions):
    return {
        "header": {
            "version": block.version,
            "hashMerkleRoot": block.hash_merkle_root,
            "acceptedIdMerkleRoot": block.accepted_id_merkle_root,
            "utxoCommitment": block.utxo_commitment,
            "timestamp": block.timestamp,
            "bits": block.bits,
            "nonce": block.nonce,
            "daaScore": block.daa_score,
            "blueWork": block.blue_work if block.blue_work else 0,
            "parents": [{"parentHashes": parents if parents else []}],
            "blueScore": block.blue_score,
            "pruningPoint": block.pruning_point,
        },
        "transactions": transactions if transactions else [],
        "verboseData": {
            "hash": block.hash,
            "difficulty": bits_to_difficulty(block.bits) if block.bits else None,
            "selectedParentHash": block.selected_parent_hash,
            "transactionIds": transaction_ids if transaction_ids else [],
            "blueScore": block.blue_score,
            "childrenHashes": children if children else [],
            "mergeSetBluesHashes": block.merge_set_blues_hashes or [],
            "mergeSetRedsHashes": block.merge_set_reds_hashes or [],
            "isChainBlock": is_chain_block,
        },
    }


def block_join_query():
    return select(
        Block,
        exists().where(TransactionAcceptance.block_hash == Block.hash),
        select(func.array_agg(BlockParent.parent_hash)).where(BlockParent.block_hash == Block.hash).scalar_subquery(),
        select(func.array_agg(BlockParent.block_hash)).where(BlockParent.parent_hash == Block.hash).scalar_subquery(),
        select(func.array_agg(BlockTransaction.transaction_id))
        .where(BlockTransaction.block_hash == Block.hash)
        .scalar_subquery(),
    )


async def get_transactions(blockId, transactionIds):
    """
    Get the transactions associated with a block
    """
    async with async_session() as s:
        transactions = (
            await s.execute(
                select(Transaction, Subnetwork)
                .join(Subnetwork, Transaction.subnetwork_id == Subnetwork.id)
                .filter(Transaction.transaction_id.in_(transactionIds))
                .order_by(Subnetwork.id)
            )
        ).all()

        tx_outputs = (
            (
                await s.execute(
                    select(TransactionOutput)
                    .where(TransactionOutput.transaction_id.in_(transactionIds))
                    .order_by(TransactionOutput.index)
                )
            )
            .scalars()
            .all()
        )

        tx_inputs = (
            (
                await s.execute(
                    select(TransactionInput)
                    .where(TransactionInput.transaction_id.in_(transactionIds))
                    .order_by(TransactionInput.index)
                )
            )
            .scalars()
            .all()
        )

    tx_list = []
    for tx, sub in transactions:
        tx_list.append(
            {
                "inputs": [
                    {
                        "previousOutpoint": {
                            "transactionId": tx_inp.previous_outpoint_hash,
                            "index": tx_inp.previous_outpoint_index,
                        },
                        "signatureScript": tx_inp.signature_script,
                        "sigOpCount": tx_inp.sig_op_count,
                    }
                    for tx_inp in tx_inputs
                    if tx_inp.transaction_id == tx.transaction_id
                ],
                "outputs": [
                    {
                        "amount": tx_out.amount,
                        "scriptPublicKey": {"scriptPublicKey": tx_out.script_public_key, "version": 0},
                        "verboseData": {
                            "scriptPublicKeyType": tx_out.script_public_key_type,
                            "scriptPublicKeyAddress": tx_out.script_public_key_address,
                        },
                    }
                    for tx_out in tx_outputs
                    if tx_out.transaction_id == tx.transaction_id
                ],
                "subnetworkId": sub.subnetwork_id,
                "payload": tx.payload,
                "verboseData": {
                    "transactionId": tx.transaction_id,
                    "hash": tx.hash,
                    "computeMass": tx.mass,
                    "blockHash": blockId,
                    "blockTime": tx.block_time,
                },
                "mass": tx.mass,
                "version": 0,
            }
        )
    return tx_list


def convert_to_legacy_block(block: dict) -> dict:
    header = block.get("header", {})
    header["blueWork"] = header["blueWork"].lstrip("0") if header.get("blueWork") else None

    parents = []
    for level in header.get("parentsByLevel", []):
        parents.append({"parentHashes": level})
    header["parents"] = parents

    for tx in block.get("transactions", []):
        for tx_output in tx.get("outputs", []):
            tx_output["amount"] = tx_output.get("value")
            tx_output_script_public_key = tx_output.get("scriptPublicKey")
            if tx_output_script_public_key:
                tx_output_script_public_key = tx_output_script_public_key.lstrip("0")
                if len(tx_output_script_public_key) % 2 == 1:
                    tx_output_script_public_key = "0" + tx_output_script_public_key
                tx_output["scriptPublicKey"] = {
                    "scriptPublicKey": tx_output_script_public_key,
                    "version": 0,
                }
            tx_output_verbose_data = tx_output.get("verboseData")
            if tx_output_verbose_data:
                script_public_key_type = tx_output_verbose_data.get("scriptPublicKeyType")
                if script_public_key_type:
                    tx_output_verbose_data["scriptPublicKeyType"] = script_public_key_type.lower()
    return block
