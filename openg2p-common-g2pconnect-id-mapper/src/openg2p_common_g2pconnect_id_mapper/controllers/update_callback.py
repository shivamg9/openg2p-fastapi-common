import logging
import uuid
from datetime import datetime

import orjson
import redis.asyncio as redis_asyncio
from openg2p_fastapi_common.controller import BaseController
from openg2p_fastapi_common.errors.base_error import ErrorResponse

from ..config import Settings
from ..context import queue_redis_async_pool
from ..models.common import (
    Ack,
    CommonResponse,
    CommonResponseMessage,
    RequestStatusEnum,
    TxnStatus,
)
from ..models.update import UpdateCallbackHttpRequest
from ..service.update import MapperUpdateService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class UpdateCallbackController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.mapper_update_service = MapperUpdateService.get_component()

        self.router.prefix += _config.callback_api_common_prefix
        self.router.tags += ["callback"]

        self.router.add_api_route(
            "/mapper/on-update",
            self.mapper_on_update,
            methods=["POST"],
            responses={200: {"model": CommonResponseMessage}},
        )

    async def mapper_on_update(self, update_http_request: UpdateCallbackHttpRequest):
        """
        The API that ID Mapper calls back when a ID Mapper Update Request is made.
        - Returns positive ACK (acc to G2P Connect Spec) if the txn_id is known.
          Return negative ACK otherwise.
        """
        txn_id = update_http_request.message.transaction_id
        queue = redis_asyncio.Redis(connection_pool=queue_redis_async_pool.get())

        if not await queue.exists(f"{_config.queue_update_name}{txn_id}"):
            _logger.error("On Update. Invalid Txn id received.")
            return CommonResponseMessage(
                message=CommonResponse(
                    ack_status=Ack.NACK,
                    timestamp=datetime.utcnow(),
                    correlation_id=str(uuid.uuid4()),
                    error=ErrorResponse(
                        code="rjct.transaction.id.invalid",
                        message="Unknown transaction id.",
                    ),
                )
            )

        txn_status = TxnStatus.model_validate(
            orjson.loads(await queue.get(f"{_config.queue_update_name}{txn_id}"))
        )
        txn_status.status = update_http_request.header.status

        for txn in update_http_request.message.update_response:
            txn_status.refs[txn.reference_id].status = txn.status
            _logger.debug(
                "On Update. Received callback, status: %s, code: %s, message: %s",
                txn.status,
                txn.status_reason_code,
                txn.status_reason_message,
            )
            if txn.status_reason_code:
                txn_status.refs[
                    txn.reference_id
                ].status_reason_code = txn.status_reason_code.value

        if (not txn_status.status) or (txn_status.status == RequestStatusEnum.rcvd):
            # Computing txn_status if it is not returned properly.
            success_count = 0
            pending_count = 0
            for ref in txn_status.refs.values():
                if ref.status not in (RequestStatusEnum.succ, RequestStatusEnum.rjct):
                    pending_count += 1
                if ref.status == RequestStatusEnum.succ:
                    success_count += 1
            if success_count == 0 and pending_count == 0:
                txn_status.status = RequestStatusEnum.rjct
            elif pending_count == 0:
                txn_status.status = RequestStatusEnum.succ
            else:
                # TODO: Something went wrong. Pending count can not be > 0
                pass

        await queue.set(
            f"{_config.queue_update_name}{txn_id}",
            orjson.dumps(txn_status.model_dump()).decode(),
        )
        await queue.aclose()

        return CommonResponseMessage(
            message=CommonResponse(
                ack_status=Ack.ACK,
                timestamp=datetime.utcnow(),
                correlation_id=str(uuid.uuid4()),
            )
        )
