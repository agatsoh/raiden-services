import os
from typing import List
from unittest.mock import Mock

from eth_utils import decode_hex, encode_hex
from tests.constants import TEST_MSC_ADDRESS

from monitoring_service.events import ActionMonitoringTriggeredEvent
from monitoring_service.service import MonitoringService
from monitoring_service.states import HashedBalanceProof
from raiden.utils.typing import (
    BlockNumber,
    ChainID,
    ChannelID,
    Nonce,
    TokenAmount,
    TokenNetworkAddress,
)
from raiden_contracts.constants import (
    CONTRACT_MONITORING_SERVICE,
    CONTRACT_TOKEN_NETWORK_REGISTRY,
    CONTRACT_USER_DEPOSIT,
    LOCKSROOT_OF_NO_LOCKS,
)
from raiden_contracts.tests.utils import get_random_address, get_random_privkey
from raiden_libs.events import ReceiveChannelOpenedEvent, UpdatedHeadBlockEvent

from ...libs.mocks.web3 import ContractMock, Web3Mock


def test_crash(
    tmpdir, get_accounts, get_private_key, mockchain
):  # pylint: disable=too-many-locals
    """ Process blocks and compare results with/without crash

    A somewhat meaninful crash handling is simulated by not including the
    UpdatedHeadBlockEvent in every block.
    """
    channel_identifier = ChannelID(3)
    c1, c2 = get_accounts(2)
    token_network_address = TokenNetworkAddress(decode_hex(get_random_address()))
    balance_proof = HashedBalanceProof(
        nonce=Nonce(1),
        transferred_amount=TokenAmount(2),
        priv_key=get_private_key(c1),
        channel_identifier=channel_identifier,
        token_network_address=token_network_address,
        chain_id=ChainID(1),
        additional_hash="0x%064x" % 0,
        locked_amount=0,
        locksroot=encode_hex(LOCKSROOT_OF_NO_LOCKS),
    )
    monitor_request = balance_proof.get_monitor_request(
        get_private_key(c2), reward_amount=TokenAmount(0), msc_address=TEST_MSC_ADDRESS
    )

    events = [
        [
            ReceiveChannelOpenedEvent(
                token_network_address=token_network_address,
                channel_identifier=channel_identifier,
                participant1=c1,
                participant2=c2,
                settle_timeout=20,
                block_number=BlockNumber(0),
            )
        ],
        [UpdatedHeadBlockEvent(BlockNumber(1))],
        [
            ActionMonitoringTriggeredEvent(
                token_network_address=token_network_address,
                channel_identifier=channel_identifier,
                non_closing_participant=c2,
            )
        ],
        [UpdatedHeadBlockEvent(BlockNumber(3))],
    ]
    mockchain(events)

    server_private_key = get_random_privkey()

    contracts = {
        CONTRACT_TOKEN_NETWORK_REGISTRY: ContractMock(),
        CONTRACT_MONITORING_SERVICE: ContractMock(),
        CONTRACT_USER_DEPOSIT: ContractMock(),
    }

    def new_ms(filename):
        ms = MonitoringService(
            web3=Web3Mock(),
            private_key=server_private_key,
            contracts=contracts,
            db_filename=os.path.join(tmpdir, filename),
        )
        msc = Mock()
        ms.context.monitoring_service_contract = msc
        ms.monitor_mock = msc.functions.monitor.return_value.transact  # type: ignore
        ms.monitor_mock.return_value = bytes(0)  # type: ignore
        return ms

    # initialize both monitoring services
    stable_ms = new_ms("stable.db")
    crashy_ms = new_ms("crashy.db")
    for ms in [stable_ms, crashy_ms]:
        ms.database.conn.execute(
            "INSERT INTO token_network(address) VALUES (?)", [token_network_address]
        )
        ms.context.ms_state.blockchain_state.token_network_addresses = [token_network_address]
        ms.database.upsert_monitor_request(monitor_request)
        ms.database.conn.commit()

    # process each block and compare results between crashy and stable ms
    for to_block in range(len(events)):
        crashy_ms = new_ms("crashy.db")  # new instance to simulate crash
        stable_ms.monitor_mock.reset_mock()  # clear calls from last block
        result_state: List[dict] = []
        for ms in [stable_ms, crashy_ms]:
            ms._process_new_blocks(to_block)  # pylint: disable=protected-access
            result_state.append(
                dict(
                    blockchain_state=ms.context.ms_state.blockchain_state,
                    db_dump=list(ms.database.conn.iterdump()),
                    monitor_calls=ms.monitor_mock.mock_calls,
                )
            )

        # both instances should have the same state after processing
        for stable_state, crashy_state in zip(result_state[0].values(), result_state[1].values()):
            # do asserts for each key separately to get better error messages
            assert stable_state == crashy_state
