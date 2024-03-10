from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union, cast
from asyncio import Queue
from datetime import datetime
from enum import Enum
import asyncio
import json
import time

from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.api import Method
from mautrix.errors.request import MatrixRequestError, MForbidden, MTooLarge
from mautrix.types import (
    EventID,
    EventType,
    ExtensibleEnum,
    Format,
    MessageType,
    PaginatedMessages,
    PaginationDirection,
    ReactionEvent,
    RoomID,
    StateEvent,
    SyncToken,
    TextMessageEventContent,
)
from mautrix.util import markdown
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from federationbot.events import (
    Event,
    EventBase,
    EventError,
    GenericStateEvent,
    RoomMemberStateEvent,
    determine_what_kind_of_event,
)
from federationbot.federation import (
    FederationHandler,
    authorization_headers,
    filter_events_based_on_type,
    filter_state_events_based_on_membership,
    parse_list_response_into_list_of_event_bases,
)
from federationbot.responses import (
    FederationBaseResponse,
    FederationErrorResponse,
    FederationServerKeyResponse,
    FederationVersionResponse,
)
from federationbot.server_result import (
    DiagnosticInfo,
    ResponseStatusType,
    ServerResult,
    ServerResultError,
)
from federationbot.utils import (
    DisplayLineColumnConfig,
    Justify,
    get_domain_from_id,
    pad,
)

# An event is considered having a maximum size of 64K. Unfortunately, encryption uses
# more space than cleartext, so give some slack room
MAX_EVENT_SIZE_FOR_SENDING = 40000
# For 'whole room' commands, limit the maximum number of servers to even try
MAX_NUMBER_OF_SERVERS_TO_ATTEMPT = 400

# number of concurrent requests to a single server
MAX_NUMBER_OF_CONCURRENT_TASKS = 10
# number of servers to make requests to concurrently
MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST = 100

SECONDS_BETWEEN_EDITS = 5.0

# Column headers. Probably will remove these constants
SERVER_NAME = "Server Name"
SERVER_SOFTWARE = "Software"
SERVER_VERSION = "Version"
CODE = "Code"

NOT_IN_ROOM_ERROR = (
    "Cannot process for a room I'm not in. Invite this bot to that room and try again."
)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("whitelist")
        helper.copy("server_signing_keys")


class CommandType(Enum):
    avoid_excess = "avoid_excess"
    all = "all"
    count = "count"


json_decoder = json.JSONDecoder()


def is_event_id(maybe_event_id: str) -> Optional[str]:
    if maybe_event_id.startswith("$"):
        return maybe_event_id
    else:
        return None


def is_room_id(maybe_room_id: str) -> Optional[str]:
    if maybe_room_id.startswith("!"):
        return maybe_room_id
    else:
        return None


def is_room_alias(maybe_room_alias: str) -> Optional[str]:
    if maybe_room_alias.startswith("#"):
        return maybe_room_alias
    else:
        return None


def is_room_id_or_alias(maybe_room: str) -> Optional[str]:
    result = is_room_id(maybe_room)
    if result:
        return result
    result = is_room_alias(maybe_room)
    return result


def is_mxid(maybe_mxid: str) -> Optional[str]:
    if maybe_mxid.startswith("@"):
        return maybe_mxid
    else:
        return None


def is_int(maybe_int: str) -> Optional[int]:
    try:
        result = int(maybe_int)
    except ValueError:
        return None
    else:
        return result


def is_command_type(maybe_subcommand: str) -> Optional[str]:
    if maybe_subcommand in CommandType:
        return maybe_subcommand
    else:
        return None


class ReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    START = "Start "
    PAUSE = "Pause "
    STOP = "Stop "


class FederationBot(Plugin):
    task_control: Dict[EventID, ReactionCommandStatus]
    reaction_handler_count: int

    cached_servers: Dict[str, str]
    server_signing_keys: Dict[str, str]
    federation_handler: FederationHandler

    @classmethod
    def get_config_class(cls) -> Union[Type[BaseProxyConfig], None]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.server_signing_keys = {}
        self.task_control: Dict[EventID, ReactionCommandStatus] = {}
        self.reaction_handler_count = 0
        if self.config:
            self.config.load_and_update()
            # self.log.info(str(self.config["server_signing_keys"]))
            for server, key_data in self.config["server_signing_keys"].items():
                self.server_signing_keys[server] = key_data
        self.federation_handler = FederationHandler(
            self.http, self.log, self.server_signing_keys
        )

    async def pre_stop(self) -> None:
        self.client.remove_event_handler(EventType.REACTION, self.react_control_handler)

    async def react_control_handler(self, react_evt: ReactionEvent) -> None:
        reaction_data = react_evt.content.relates_to
        if (
            react_evt.sender != self.client.mxid
            and reaction_data.event_id in self.task_control
        ):
            if reaction_data.key == ReactionCommandStatus.STOP.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.STOP
            elif reaction_data.key == ReactionCommandStatus.PAUSE.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.PAUSE
            elif reaction_data.key == ReactionCommandStatus.START.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.START

        return

    @command.new(
        name="test",
        help="playing",
        arg_fallthrough=True,
    )
    async def test_command(
        self,
        command_event: MessageEvent,
    ) -> None:
        await command_event.respond(f"Received Test Command on: {self.client.mxid}")

    @test_command.subcommand(
        name="context",
        help="level 1",
    )
    @command.argument(name="room_id_or_alias", parser=is_room_id, required=True)
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="limit", required=False)
    async def context_subcommand(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str,
        event_id: str,
        limit: str,
    ) -> None:
        stuff = await self.client.get_event_context(
            room_id=RoomID(room_id_or_alias),
            event_id=EventID(event_id),
            limit=int(limit),
        )
        await command_event.respond(stuff.json())

    @test_command.subcommand(
        name="room_walk",
        help="Use the /message client endpoint to force fresh state download(beta).",
    )
    @command.argument(name="room_id_or_alias", required=False)
    @command.argument(name="per_iteration", required=False)
    async def room_walk_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        per_iteration: str = "1000",
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if get_domain_from_id(command_event.sender) != get_domain_from_id(
            self.client.mxid
        ):
            await command_event.reply(
                "I'm sorry, running this command from a user not on the same server as the bot will not help"
            )
            return

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        # Sort out the room id
        if room_id_or_alias:
            room_to_check = await self._resolve_room_id_or_alias(
                room_id_or_alias, command_event, origin_server
            )
            if not room_to_check:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_to_check = command_event.room_id

        try:
            per_iteration_int = int(per_iteration)
        except ValueError:
            await command_event.reply("per_iteration must be an integer")
            return

        # Need:
        # *1. to get the current depth for the room, so we have an idea how many events
        # we need to collect
        # *2. progress bars for backwalk based on depth
        # *3. total count of:
        #    a. discovered events
        #    b. new events found on backwalk
        # 4. itemized display of type of events found, and that are new
        # 5. total time spent on discovery and backwalk
        # 6. rolling time spent on backwalk requests, or maybe just fastest and longest
        # 7. event count of what is new

        # Want it to look like:
        #
        # Room depth reported as: 45867
        # Events found during discovery: 38757
        #   Time taken: 50 seconds
        # [|||||||||||||||||||||||||||||||||||||||||||||||||] 100%
        # New Events found during backwalk: 0(0 State)
        #   Time taken: 120 seconds
        #

        # Get the last event that was in the room, for it's depth
        now = int(time.time() * 1000)
        time_to_check = now
        room_depth = 0
        ts_response = await self.federation_handler.get_timestamp_to_event_from_server(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_to_check,
            utc_time_at_ms=time_to_check,
        )
        if isinstance(ts_response, FederationErrorResponse):
            await command_event.respond(
                "Something went wrong while getting last event in room("
                f"{ts_response.reason}"
                "). Please supply an event_id instead at the place in time of query"
            )
            return
        else:
            room_depth = ts_response.response_dict.get("depth", 0)

        # Initial messages and lines setup. Never end in newline, as the helper handles
        header_lines = ["Room Back-walking Procedure: Running"]
        static_lines = []
        static_lines.extend(["--------------------------"])
        static_lines.extend([f"Room Depth reported as: {room_depth}"])

        discovery_lines: List[str] = []
        progress_line = ""
        backwalk_lines: List[str] = []

        def _combine_lines_for_backwalk() -> str:
            combined_lines = ""
            for line in header_lines:
                combined_lines += line + "\n"
            for line in static_lines:
                combined_lines += line + "\n"
            for line in discovery_lines:
                combined_lines += line + "\n"
            combined_lines += progress_line + "\n"
            for line in backwalk_lines:
                combined_lines += line + "\n"

            return combined_lines

        pinned_message = await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk())
            )
        )

        async def _inner_walking_fetcher(
            for_direction: PaginationDirection, queue: Queue
        ) -> None:
            retry_token = False
            retry_count = 0
            back_off_time = 0.0
            next_token = None
            while True:
                if not retry_token:
                    back_off_time, next_token = await queue.get()

                if back_off_time > 1.0:
                    self.log.warning(f"Backing off for {back_off_time}")
                    await asyncio.sleep(back_off_time)

                try:
                    iter_start_time = time.time()
                    worker_response = await self.client.get_messages(
                        room_id=RoomID(room_to_check),
                        direction=for_direction,
                        from_token=next_token,
                        limit=per_iteration_int,
                        # filter_json=,
                    )
                    iter_finish_time = time.time()
                except MatrixRequestError as e:
                    self.log.warning(f"{e}")
                    retry_token = True
                else:
                    retry_token = False
                    _time_spent = iter_finish_time - iter_start_time
                    response_list.extend([(_time_spent, worker_response)])

                    # prep for next iteration
                    if getattr(worker_response, "end"):
                        # The queue item is (new_back_off_time, pagination_token
                        queue.put_nowait((_time_spent * 0.5, worker_response.end))

                    # Don't want this behind a 'finally', as it should only run if not retrying the request
                    queue.task_done()

        discovery_iterations = 0
        discovery_cumulative_iter_time = 0.0
        discovery_collection_of_event_ids = set()
        response_list: List[Tuple[float, PaginatedMessages]] = []
        discovery_fetch_queue: Queue[Tuple[float, Optional[SyncToken]]] = Queue()

        task = asyncio.create_task(
            _inner_walking_fetcher(PaginationDirection.FORWARD, discovery_fetch_queue)
        )
        discovery_fetch_queue.put_nowait((0.0, None))
        finish = False

        while True:
            self.log.warning(f"discovery: size of response list: {len(response_list)}")
            new_responses_to_work_on = response_list.copy()
            response_list = []

            new_event_ids = set()
            for time_spent, response in new_responses_to_work_on:
                discovery_cumulative_iter_time += time_spent
                discovery_iterations = discovery_iterations + 1

                # prep for next iteration
                if getattr(response, "end"):
                    finish = False
                    # backwalk_fetch_queue.put_nowait((time_spent*0.5, response.end))
                else:
                    finish = True

                for event in response.events:
                    new_event_ids.add(event.event_id)

            discovery_collection_of_event_ids.update(new_event_ids)

            # give a status update
            discovery_total_events_received = len(discovery_collection_of_event_ids)
            discovery_lines = []
            discovery_lines.extend(
                [f"Events found during discovery: {discovery_total_events_received}"]
            )
            discovery_lines.extend(
                [
                    f"  Time taken: {discovery_cumulative_iter_time:.3f} seconds (iter# {discovery_iterations})"
                ]
            )

            # for event_type, count in discovery_event_types_count.items():
            #     discovery_lines.extend([f"{event_type}: {count}"])
            if new_responses_to_work_on or finish:
                # Only print something if there is something to say
                await command_event.respond(
                    make_into_text_event(
                        wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
                    ),
                    edits=pinned_message,
                )
            # prep for next iteration
            if finish:
                break

            await asyncio.sleep(SECONDS_BETWEEN_EDITS)

        # Cancel our worker tasks.
        task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(task, return_exceptions=True)

        backwalk_iterations = 0
        backwalk_fetch_queue: Queue[Tuple[float, Optional[SyncToken]]] = Queue()
        # List of tuples, (time_spent float, NamedTuple of data)
        response_list = []

        task = asyncio.create_task(
            _inner_walking_fetcher(PaginationDirection.BACKWARD, backwalk_fetch_queue)
        )

        backwalk_collection_of_event_ids = set()
        backwalk_count_of_new_event_ids = 0
        backwalk_cumulative_iter_time = 0.0
        finish = False
        from_token = None
        # prime the queue
        backwalk_fetch_queue.put_nowait((0.0, from_token))

        while True:
            # if this isn't needed to move the queue along, then lose it
            # await backwalk_fetch_queue.join()

            # pinch off the list of things to work on
            new_responses_to_work_on = response_list.copy()
            response_list = []

            new_event_ids = set()
            for time_spent, response in new_responses_to_work_on:
                backwalk_cumulative_iter_time += time_spent
                backwalk_iterations = backwalk_iterations + 1

                # prep for next iteration
                if getattr(response, "end"):
                    finish = False
                    # backwalk_fetch_queue.put_nowait((time_spent*0.5, response.end))
                else:
                    finish = True

                for event in response.events:
                    new_event_ids.add(event.event_id)

            # collect stats
            difference_of_b_and_a = new_event_ids.difference(
                backwalk_collection_of_event_ids
            )

            difference_of_bw_to_discovery = new_event_ids.difference(
                discovery_collection_of_event_ids
            )
            backwalk_count_of_new_event_ids = backwalk_count_of_new_event_ids + len(
                difference_of_bw_to_discovery
            )

            backwalk_collection_of_event_ids.update(new_event_ids)

            # setup backwalk status lines to respond
            # New Events found during backwalk: 0(0 State)
            #   Time taken: 120 seconds
            backwalk_lines = []
            progress_line = f"{len(backwalk_collection_of_event_ids)} of {room_depth}"
            backwalk_lines.extend(
                [
                    f"Received Events during backwalk: {len(backwalk_collection_of_event_ids)}"
                ]
            )
            backwalk_lines.extend(
                [f"New Events found during backwalk: {backwalk_count_of_new_event_ids}"]
            )
            backwalk_lines.extend(
                [
                    f"  Time taken: {backwalk_cumulative_iter_time:.3f} seconds (iter# {backwalk_iterations})"
                ]
            )
            backwalk_lines.extend(
                [f"  Events found this iter: ({len(difference_of_b_and_a)})"]
            )

            if new_responses_to_work_on or finish:
                # Only print something if there is something to say
                await command_event.respond(
                    make_into_text_event(
                        wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
                    ),
                    edits=pinned_message,
                )
            # prep for next iteration
            if finish:
                break

            await asyncio.sleep(SECONDS_BETWEEN_EDITS)

        # Cancel our worker tasks.
        task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(task, return_exceptions=True)
        header_lines = ["Room Back-walking Procedure: Done"]

        backwalk_lines.extend(["Done"])
        await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
            ),
            edits=pinned_message,
        )

    @test_command.subcommand(
        name="room_hosts", help="List all hosts in a room, in order from earliest"
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="limit", parser=is_int, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def room_host_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        limit: Optional[int],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""

        # One way or another, we have a room id by now
        # assert room_id is not None

        await command_event.respond(
            f"Retrieving Hosts for \n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        host_list = await self.get_hosts_in_room_ordered(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id_in_timeline=event_id,
        )

        # Time to start rendering. Build the header lines first
        header_message = "Hosts in order of state membership joins\n"

        list_of_buffer_lines = []

        if limit:
            # if limit is more than the number of hosts, fix it
            limit = min(limit, len(host_list))
            for host_number in range(0, limit):
                list_of_buffer_lines.extend(
                    [f"{host_list[host_number:host_number+1]}\n"]
                )
        else:
            for host in host_list:
                list_of_buffer_lines.extend([f"['{host}']\n"])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @test_command.subcommand(
        name="find_event", help="Search all hosts in a given room for a given Event"
    )
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=True
    )
    async def find_event_command(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        room_id_or_alias: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        room_id = await self._resolve_room_id_or_alias(
            room_id_or_alias, command_event, origin_server
        )
        if not room_id:
            # Don't need to actually display an error, that's handled in the above
            # function
            return

        await command_event.respond(
            f"Checking all hosts for:\n"
            f"* Event ID: {event_id}\n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* Using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        # Get all the hosts in the room
        host_list = await self.get_hosts_in_room_ordered(
            origin_server, origin_server, room_id, event_id
        )
        use_ordered_list = True
        if not host_list:
            use_ordered_list = False
            # Either the origin server doesn't have the state, or some other problem
            # occurred. Fall back to the client api with current state. Obviously there
            # are problems with this, but it will allow forward progress.
            await command_event.respond(
                "Failed getting hosts from State over federation, "
                "falling back to client API"
            )
            try:
                joined_members = await self.client.get_joined_members(RoomID(room_id))

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    host = get_domain_from_id(member)
                    if host not in host_list:
                        host_list.extend([host])

        host_queue = Queue()
        for host in host_list:
            host_queue.put_nowait(host)

        host_to_event_status_map: Dict[str, EventBase] = {}

        async def _event_finding_worker(queue: Queue) -> None:
            while True:
                worker_host = await queue.get()
                returned_events = await self.federation_handler.get_event_from_server(
                    origin_server=origin_server,
                    destination_server=worker_host,
                    event_id=event_id,
                )
                inner_returned_event = returned_events.get(event_id)

                host_to_event_status_map[worker_host] = inner_returned_event
                queue.task_done()

        # Collect all the Federation Responses as well as the EventBases.
        # Errors can be found in the Responses.

        tasks_list = []
        for _ in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            tasks_list.append(asyncio.create_task(_event_finding_worker(host_queue)))

        started_at = time.time()
        await host_queue.join()
        total_time = time.time() - started_at
        # Cancel our worker tasks.
        for task in tasks_list:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks_list, return_exceptions=True)

        # Begin the render
        dc_host_config = DisplayLineColumnConfig("Hosts", justify=Justify.RIGHT)
        dc_result_config = DisplayLineColumnConfig("Results")

        for host, result in host_to_event_status_map.items():
            dc_host_config.maybe_update_column_width(len(host))

        header_message = (
            f"Hosts{'(in oldest order)' if use_ordered_list else ''} that found "
            f"event '{event_id}'\n"
        )
        list_of_result_data = []
        for host in host_list:
            result = host_to_event_status_map.get(host)
            buffered_message = ""
            if result:
                if isinstance(result, EventError):
                    buffered_message += (
                        f"{dc_host_config.pad(host)}"
                        f"{dc_host_config.horizontal_separator}"
                        f"{dc_result_config.pad('Fail')}"
                        f"{dc_host_config.horizontal_separator}{result.error}"
                    )
                else:
                    buffered_message += (
                        f"{dc_host_config.pad(host)}"
                        f"{dc_host_config.horizontal_separator}"
                        f"{dc_result_config.pad('OK')}"
                    )
            else:
                # The "unlikely to ever be hit" error
                buffered_message += (
                    f"{dc_host_config.pad(host)}"
                    f"{dc_host_config.horizontal_separator}"
                    f"{dc_result_config.pad('Fail')}"
                    f"{dc_host_config.horizontal_separator}"
                    "Plugin error(Host not contacted)"
                )

            list_of_result_data.extend([f"{buffered_message}\n"])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    # I think the command map should look a little like this:
    # (defaults will be marked with * )
    # !fed
    #   - state
    #       - avoid_excess* [room_id][event_id]  - retrieve state at this specific
    #                                      event_id(or last event) from room_id(or
    #                                      current room) but do not include similar
    #                                      events if they count more than 10 of each
    #       - all   [room_id][event_id]
    #       - count
    #   - event   [event_id] - retrieve a specific event(or last in room)
    #   - events  [room_id][how_many]   - retrieve the last how_many(or 10) events from
    #                                       room_id(or current room)

    @command.new(name="fed", help="`!fed`: Federation requests for information")
    async def fed_command(self, command_event: MessageEvent) -> None:
        pass

    @fed_command.subcommand(
        name="summary", help="Print summary of the delegation portion of the spec"
    )
    async def summary(self, command_event: MessageEvent) -> None:
        await command_event.mark_read()

        await command_event.respond(
            "Summary of how Delegation is processed for a Matrix homeserver.\n"
            "The process to determine the ultimate final host:port is defined in "
            "the [spec](https://spec.matrix.org/v1.9/server-server-api/#resolving-"
            "server-names)\n"
            + wrap_in_code_block_markdown(
                "Basically:\n"
                "1. If it's a literal IP, then use that either with the port supplied "
                "or 8448\n"
                "2. If it's a hostname with an explicit port, resolve with DNS to an "
                "A, AAAA or CNAME record\n"
                "3. If it's a hostname with no explicit port, request from\n"
                "   <server_name>/.well-known/matrix/server and parse the json. "
                "Anything\n"
                "   wrong, skip to step 4. Want "
                "<delegated_server_name>[:<delegated_port>]\n"
                "   3a. Same as 1 above, except don't just use 8448(step 3e)\n"
                "   3b. Same as 2 above.\n"
                "   3c. If no explicit port, check for a SRV record at\n"
                "       _matrix-fed._tcp.<delegated_server_name> to get the port "
                "number.\n"
                "       Resolve with A or AAAA(but not CNAME) record\n"
                "   3d. (deprecated) Check _matrix._tcp.<delegated_server_name> "
                "instead\n"
                "   3e. (there was no port, remember), resolve using provided "
                "delegated\n"
                "       hostname and use port 8448\n"
                "4. (no well-known) Check SRV record(same as 3c above)\n"
                "5. (deprecated) Check other SRV record(same as 3d above)\n"
                "6. Use the supplied server_name and try port 8448\n"
            )
        )

    @command.new(
        name="delegation",
        help="Some simple diagnostics around federation server discovery",
    )
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def delegation_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            # Only sub commands display the 'help' text field(for now at least). Tell
            # them how it works.
            await command_event.reply(
                "**Usage**: !delegation <server_name>\n - Some simple diagnostics "
                "around federation server discovery"
            )
            return

        await self._delegations(command_event, server_to_check)

    async def _delegations(
        self,
        command_event: MessageEvent,
        server_to_check: str,
    ) -> None:
        list_of_servers_to_check = set()

        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_to_check = command_event.room_id

        # server_to_check has survived this far, add it to the set of servers to search
        # for. Since we allow for searching an entire room, it will be the only server
        # in the set.
        if server_to_check:
            list_of_servers_to_check.add(server_to_check)

        # The list of servers was empty. This implies that a room_id was provided,
        # let's check.
        if not list_of_servers_to_check:
            try:
                joined_members = await self.client.get_joined_members(room_to_check)

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))

        # The first of the 'entire room' limitations
        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        # Some quality of life niceties
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete."
        )

        # map of server name -> (server brand, server version)
        server_to_server_data: Dict[str, FederationBaseResponse] = {}

        async def _delegation_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    # The 'get_server_version' function was written with the capability of
                    # collecting diagnostic data.
                    server_to_server_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_version(
                            worker_server_name,
                            force_recheck=True,
                            diagnostics=True,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Request timed out",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )
                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        delegation_queue = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await delegation_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_delegation_worker(delegation_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await delegation_queue.join()
        # await asyncio.gather(
        #     *[_delegation(server_name) for server_name in list_of_servers_to_check]
        # )
        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Want the full room version it to look like this for now
        #
        #   Server Name | WK   | SRV  | DNS  | Test  | SNI | TLS served by  |
        # ------------------------------------------------------------------
        #   example.org | OK   | None | OK   | OK    |     | Synapse 1.92.0 |
        # somewhere.net | None | None | None | Error |     | resty          | Long error....
        #   maunium.net | OK   | OK   | OK   | OK    | SNI | Caddy          |

        # The single server version will be the same in that a single line like above
        # will be printed, then the rendered diagnostic data

        # Create the columns to be used
        server_name_col = DisplayLineColumnConfig("Server Name")
        well_known_status_col = DisplayLineColumnConfig("WK")
        srv_status_col = DisplayLineColumnConfig("SRV")
        dns_status_col = DisplayLineColumnConfig("DNS")
        connective_test_status_col = DisplayLineColumnConfig("Test")
        tls_served_by_col = DisplayLineColumnConfig("TLS served by")

        # Iterate through the server names to widen the column, if necessary.
        for server_name, server_results in server_to_server_data.items():
            server_name_col.maybe_update_column_width(len(server_name))
            if not isinstance(server_results, FederationErrorResponse):
                tls_server = server_results.headers.get("server", None)
                if tls_server:
                    tls_served_by_col.maybe_update_column_width(len(tls_server))

        # Just use a fixed width for the results. Should never be larger than 5 for most
        well_known_status_col.maybe_update_column_width(5)
        srv_status_col.maybe_update_column_width(5)
        dns_status_col.maybe_update_column_width(5)
        connective_test_status_col.maybe_update_column_width(5)

        # Begin constructing the message
        #
        # Use a sorted list of server names, so it displays in alphabetical order.
        server_results_sorted = sorted(server_to_server_data.keys())

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{well_known_status_col.pad()} | "
            f"{srv_status_col.pad()} | "
            f"{dns_status_col.pad()} | "
            f"{connective_test_status_col.pad()} | "
            f"{tls_served_by_col.pad()} | "
            f"Errors\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            server_response = server_to_server_data.get(server_name, None)

            if server_response:
                # Shortcut reference the diag_info to cut down line length
                diag_info = server_response.server_result.diag_info

                # The server name column
                buffered_message = f"{server_name_col.front_pad(server_name)} | "
                # The well-known status column
                buffered_message += (
                    f"{well_known_status_col.pad(diag_info.get_well_known_status())} | "
                )

                # the SRV record status column
                buffered_message += (
                    f"{srv_status_col.pad(diag_info.get_srv_record_status())} | "
                )

                # the DNS record status column
                buffered_message += (
                    f"{dns_status_col.pad(diag_info.get_dns_record_status())} | "
                )

                # The connectivity status column
                connectivity_status = diag_info.get_connectivity_test_status()
                buffered_message += (
                    f"{connective_test_status_col.pad(connectivity_status)} | "
                )
                if not isinstance(server_response, FederationErrorResponse):
                    error_reason = None
                    reverse_proxy = server_response.headers.get("server", None)
                else:
                    error_reason = server_response.reason
                    reverse_proxy = None

                buffered_message += (
                    f"{tls_served_by_col.pad(reverse_proxy if reverse_proxy else '')}"
                    " | "
                )
                buffered_message += f"{error_reason if error_reason else ''}"

                buffered_message += "\n"
                if number_of_servers == 1:
                    # Print the diagnostic summary, since there is only one server there
                    # is no need to be brief.
                    buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"
                    for line in diag_info.list_of_results:
                        buffered_message += f"{pad('', 3)}{line}\n"

                    buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"

                list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(name="event_raw")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def event_command(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from "
            f"{destination_server} using {origin_server}"
        )

        # Collect all the Federation Responses as well as the EventBases.
        # Errors can be found in the Responses.
        returned_events = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )

        buffered_message = ""
        returned_event = returned_events.get(event_id)
        if isinstance(returned_event, EventError):
            buffered_message += (
                f"received an error\n{returned_event.errcode}:{returned_event.error}"
            )

        else:
            buffered_message += f"{returned_event.event_id}\n"
            # EventBase.to_json() does not have a trailing new line, add one
            buffered_message += returned_event.to_json() + "\n"

        # It is extremely unlikely that an Event will be larger than can be displayed.
        # Don't bother chunking the response.
        try:
            await command_event.respond(wrap_in_code_block_markdown(buffered_message))
        except MTooLarge:
            await command_event.respond("Somehow, Event is to large to display")

    @fed_command.subcommand(name="event")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def event_command_pretty(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from "
            f"{destination_server} using {origin_server}"
        )

        returned_event_dict = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )

        buffered_message = ""
        returned_event = returned_event_dict.get(event_id)
        if isinstance(returned_event, EventError):
            buffered_message += (
                f"received an error\n{returned_event.errcode}:{returned_event.error}\n"
            )

        else:
            # a_event will stand for ancestor event
            # A mapping of 'a_event_id' to the string of short data about the a_event to
            # be shown
            a_event_data_map: Dict[str, str] = {}
            # Recursively retrieve events that are in the immediate past. This
            # allows for some annotation to the events when they are displayed in
            # the 'footer' section of the rendered response. For example: auth
            # events will have their event type displayed, such as 'm.room.create'
            # and the room version.
            list_of_a_event_ids = returned_event.auth_events.copy()
            list_of_a_event_ids.extend(returned_event.prev_events)

            a_returned_events = await self.federation_handler.get_events_from_server(
                origin_server=origin_server,
                destination_server=destination_server,
                events_list=list_of_a_event_ids,
            )
            for a_event_id in list_of_a_event_ids:
                a_event_base = a_returned_events.get(a_event_id)
                if a_event_base:
                    a_event_data_map[a_event_id] = a_event_base.to_short_type_summary()

            # Begin rendering
            buffered_message += returned_event.to_pretty_summary()
            # Add a little gap at the bottom of the previous for better separation
            buffered_message += "\n"
            buffered_message += returned_event.to_pretty_summary_content()
            buffered_message += returned_event.to_pretty_summary_unrecognized()
            buffered_message += returned_event.to_pretty_summary_footer(
                event_data_map=a_event_data_map
            )

        await command_event.respond(wrap_in_code_block_markdown(buffered_message))

    @fed_command.subcommand(
        name="state", help="Request state over federation for a room."
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def state_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""
        await command_event.respond(
            f"Retrieving State for:\n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        # This will retrieve the events and the auth chain, we only use the former here
        (pdu_list, _,) = await self.federation_handler.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )

        await command_event.respond(
            f"Retrieving {len(pdu_list)} events from {destination_server}"
        )

        # Keep both the response and the actual event, if there was an error it will be
        # in the response and the event won't exist here
        event_to_event_base: Dict[str, EventBase]

        started_at = time.monotonic()
        event_to_event_base = await self.federation_handler.get_events_from_server(
            origin_server, destination_server, pdu_list
        )
        total_time = time.monotonic() - started_at

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_eid = DisplayLineColumnConfig("Event ID")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")

        # Preprocessing:
        # 1. Set the column widths
        # 2. Get the depth's for row ordering
        list_of_event_ids: List[Tuple[int, EventID]] = []
        for event_id, event_id_entry in event_to_event_base.items():
            list_of_event_ids.append((event_id_entry.depth, EventID(event_id)))

            dc_depth.maybe_update_column_width(len(str(event_id_entry.depth)))
            dc_eid.maybe_update_column_width(len(event_id))
            dc_etype.maybe_update_column_width(len(event_id_entry.event_type))
            dc_sender.maybe_update_column_width(len(event_id_entry.sender))

        # Sort the list in place by the first of the tuples, which is the depth
        list_of_event_ids.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_eid.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Use the sorted list to pull the events in order and begin the render
        for (_, event_id) in list_of_event_ids:
            buffered_message = ""
            event_base = event_to_event_base.get(event_id, None)
            if event_base:
                line_summary = event_base.to_line_summary(
                    dc_depth=dc_depth,
                    dc_eid=dc_eid,
                    dc_etype=dc_etype,
                    dc_sender=dc_sender,
                )
                buffered_message += f"{line_summary}\n"
            else:
                buffered_message += f"{event_id} was not found(unknown reason)\n"

            list_of_buffer_lines.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_buffer_lines.extend([footer_message])
        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="version",
        aliases=["versions"],
        help="Check a server in the room for version info",
    )
    @command.argument(name="server_to_check", label="Server to check", required=True)
    async def version(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ):
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed version <server_name>\n - Check a server in the room "
                "for version info"
            )
            return
        await self._versions(command_event, server_to_check)

    async def _versions(
        self,
        command_event: MessageEvent,
        server_to_check: str,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        list_of_servers_to_check = set()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            # Get the members this bot knows about in this room
            # TODO: try and find a way to not use the client API for this
            try:
                joined_members = await self.client.get_joined_members(room_to_check)

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        # Guard against there being to many servers on the response
        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}"
        )

        # map of server name -> (server brand, server version)
        server_to_version_data: Dict[str, FederationBaseResponse] = {}

        async def _version_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_version_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_version(
                            worker_server_name,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    server_to_version_data[
                        worker_server_name
                    ] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Timed out waiting for response",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )
                except Exception as e:
                    server_to_version_data[
                        worker_server_name
                    ] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        version_queue = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await version_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_version_worker(version_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await version_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Establish the initial size of the padding for each row
        server_name_col = DisplayLineColumnConfig(SERVER_NAME)
        server_software_col = DisplayLineColumnConfig(SERVER_SOFTWARE)
        server_version_col = DisplayLineColumnConfig(SERVER_VERSION)

        # Iterate over all the data to collect the column sizes
        for server, result in server_to_version_data.items():
            server_name_col.maybe_update_column_width(len(server))

            if isinstance(result, FederationErrorResponse):
                server_software_col.maybe_update_column_width(
                    len(str(result.status_code))
                )
                server_version_col.maybe_update_column_width(len(str(result.reason)))
            else:
                assert isinstance(result, FederationVersionResponse)
                server_software_col.maybe_update_column_width(
                    len(result.server_software)
                )
                server_version_col.maybe_update_column_width(len(result.server_version))

        # Construct the message response now
        #
        # Want it to look like
        #         Server Name | Software | Version
        # -------------------------------------------------------------------------
        #         example.org | Synapse  | 1.98.0
        #          matrix.org | Synapse  | 1.99.0rc1 (b=matrix-org-hotfixes,4d....)
        # dendrite.matrix.org | Dendrite | 0.13.5+13c5173

        # Obviously, a single server will have only one line

        # Create the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_software_col.pad()} | "
            f"{server_version_col.pad()}\n"
        )

        # Create the delimiter line
        header_message_line_size = len(header_message)
        header_message += f"{pad('', header_message_line_size, pad_with='-')}\n"

        # Alphabetical looks nicer
        sorted_list_of_servers = sorted(list_of_servers_to_check)

        # Collect all the output lines for chunking afterward
        list_of_result_data = []

        for server_name in sorted_list_of_servers:
            buffered_message = ""
            server_data = server_to_version_data.get(server_name, None)

            buffered_message += f"{server_name_col.front_pad(server_name)} | "
            # Federation request may have had an error, handle those errors here
            if isinstance(server_data, FederationErrorResponse):
                # Pad the software column with spaces, so the error and the code end up in the version column
                buffered_message += f"{server_software_col.pad('')} | "

                # status codes of 0 represent the kind of error that doesn't have an
                # http code, like an SSL error.
                if server_data.status_code > 0:
                    buffered_message += f"{server_data.status_code}:"

                buffered_message += f"{server_data.reason}\n"
            else:
                assert isinstance(server_data, FederationVersionResponse)
                buffered_message += (
                    f"{server_software_col.pad(server_data.server_software)} | "
                    f"{server_data.server_version}\n"
                )

            list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        # Wrap in code block markdown before sending
        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(name="server_keys")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed server_keys <server_name>\n - Check a server in the "
                "room for version info"
            )
            return
        await self._server_keys(command_event, server_to_check)

    @fed_command.subcommand(name="server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_raw_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed server_keys <server_name>\n - Check a server in the "
                "room for version info"
            )
            return
        await self._server_keys(command_event, server_to_check, display_raw=True)

    async def _server_keys(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        display_raw: bool = False,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        list_of_servers_to_check = set()
        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            try:
                joined_members = await self.client.get_joined_members(room_to_check)

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        server_to_server_data: Dict[str, FederationBaseResponse] = {}
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}"
        )

        async def _server_keys_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_server_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_keys(worker_server_name),
                        timeout=10.0,
                    )

                except asyncio.TimeoutError:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Request timed out",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )

                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        keys_queue = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_server_keys_worker(keys_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Want it to look like this for now
        #
        #      Server Name | Key ID | Valid until(UTC)
        # ---------------------------------------
        # littlevortex.net | aRGvs  | Pretty formatted DateTime
        #       matrix.org | aYp3g  | Pretty formatted DateTime
        #                  | 0ldK3y | EXPIRED: Expired DateTime

        server_name_col = DisplayLineColumnConfig("Server Name")
        server_key_col = DisplayLineColumnConfig("Key ID")
        valid_until_ts_col = DisplayLineColumnConfig("Valid until(UTC)")

        for server_name, server_results in server_to_server_data.items():
            server_name_col.maybe_update_column_width(len(server_name))
            verify_keys = server_results.response_dict.get("verify_keys", {})
            old_verify_keys = server_results.response_dict.get("old_verify_keys", {})
            valid_until = server_results.response_dict.get("valid_until_ts", 0)

            for key_id in verify_keys.keys():
                server_key_col.maybe_update_column_width(len(key_id))
            for key_id, old_key_data in old_verify_keys.items():
                server_key_col.maybe_update_column_width(len(key_id))
                if old_key_data:
                    valid_until_ts_col.maybe_update_column_width(
                        len(str(old_key_data.get("expired_ts", 0)))
                    )
            valid_until_ts_col.maybe_update_column_width(len(str(valid_until)))

        # Begin constructing the message

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_key_col.pad()} | "
            f"{valid_until_ts_col.header_name}\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        total_srv_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', total_srv_line_size, pad_with='-')}\n"

        # The collection of rendered lines. This will be chunked into a paged response
        list_of_result_data = []
        # Begin the data render. Use the sorted list, alphabetical looks nicer. Even
        # if there were errors, there will be data available.
        for server_name, server_response in sorted(server_to_server_data.items()):
            buffered_message = ""
            buffered_message += f"{server_name_col.front_pad(server_name)} | "

            if isinstance(server_response, FederationErrorResponse):
                buffered_message += f"{server_response.reason}\n"

            else:
                # This will be a FederationServerKeyResponse
                verify_keys = server_response.response_dict.get("verify_keys", {})
                old_verify_keys = server_response.response_dict.get(
                    "old_verify_keys", {}
                )
                valid_until_ts: Optional[int] = server_response.response_dict.get(
                    "valid_until_ts", None
                )
                valid_until_pretty = "None Found"
                if valid_until_ts:
                    valid_until_pretty = str(
                        datetime.fromtimestamp(float(valid_until_ts / 1000))
                    )

                # Use a for loop, even though there will only be a single key. I suppose
                # with the way the spec is written, multiple keys may be possible? There
                # will only be a single valid_until_ts for any of them if so.
                for key_id in verify_keys.keys():
                    buffered_message += f"{server_key_col.pad(key_id)} | "
                    buffered_message += f"{valid_until_pretty}\n"

                for key_id, key_data in old_verify_keys.items():
                    # Render the old_verify_keys inline with the normal keys. Unlike the
                    # normal keys, old_verify_keys each have an expired timestamp
                    buffered_message += f"{server_name_col.pad('')} | "
                    buffered_message += f"{server_key_col.pad(key_id)} | "
                    expired_ts: Optional[int] = old_verify_keys[key_id].get(
                        "expired_ts", None
                    )
                    expired_ts_pretty = "None Found"
                    if expired_ts:
                        expired_ts_pretty = str(
                            datetime.fromtimestamp(float(expired_ts / 1000))
                        )
                    buffered_message += f"{expired_ts_pretty}\n"

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                list_of_result_data.extend(
                    [f"{json.dumps(server_response.response_dict, indent=4)}\n"]
                )

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(name="notary_server_keys")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_command(
        self,
        command_event: MessageEvent,
        server_to_check: Optional[str],
        notary_server_to_use: Optional[str],
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed notary_server_keys <server_name> [notary_to_ask]\n"
                " - Check a server in the room for version info"
            )
            return
        await self._server_keys_from_notary(
            command_event, server_to_check, notary_server_to_use=notary_server_to_use
        )

    @fed_command.subcommand(name="notary_server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_raw_command(
        self,
        command_event: MessageEvent,
        server_to_check: Optional[str],
        notary_server_to_use: Optional[str],
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed notary_server_keys <server_name> [notary_to_ask]\n"
                " - Check a server in the room for version info"
            )
            return
        await self._server_keys_from_notary(
            command_event,
            server_to_check,
            notary_server_to_use=notary_server_to_use,
            display_raw=True,
        )

    async def _server_keys_from_notary(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        notary_server_to_use: Optional[str],
        display_raw: bool = False,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        list_of_servers_to_check = set()
        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            try:
                joined_members = await self.client.get_joined_members(room_to_check)

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                f"Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if not notary_server_to_use:
            notary_server_to_use = get_domain_from_id(command_event.sender)
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}\n"
            f"Using {notary_server_to_use}"
        )

        server_to_server_data: Dict[str, FederationBaseResponse] = {}

        async def _server_keys_from_notary_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_server_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_keys_from_notary(
                            worker_server_name, notary_server_to_use
                        ),
                        timeout=10.0,
                    )

                except asyncio.TimeoutError:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Request timed out",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )

                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin Error: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        keys_queue = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_server_keys_from_notary_worker(keys_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Preprocess the data to get the column sizes
        # Want it to look like this for now, for the whole room version. Obviously a
        # single line of the same for the 'one server' version.
        #
        #      Server Name | Key ID | Valid until(UTC)
        # ---------------------------------------
        # littlevortex.net | aRGvs  | Pretty formatted DateTime
        #       matrix.org | aYp3g  | Pretty formatted DateTime
        #                  | 0ldK3y | EXPIRED: Expired DateTime

        server_name_col = DisplayLineColumnConfig("Server Name")
        server_key_col = DisplayLineColumnConfig("Key ID")
        valid_until_ts_col = DisplayLineColumnConfig("Valid until(UTC)")

        for server_name, server_results in server_to_server_data.items():
            for server_keys in server_results.response_dict.get("server_keys", []):
                server_name_col.maybe_update_column_width(len(server_name))

                verify_keys = server_keys.get("verify_keys", {})
                old_verify_keys = server_keys.get("old_verify_keys", {})
                valid_until: Optional[int] = server_keys.get("valid_until_ts", None)
                valid_until_pretty = "None Found"

                if valid_until:
                    valid_until_pretty = str(
                        datetime.fromtimestamp(float(valid_until / 1000))
                    )

                for key_id in verify_keys.keys():
                    server_key_col.maybe_update_column_width(len(key_id))

                valid_until_ts_col.maybe_update_column_width(len(valid_until_pretty))
                for key_id, old_key_data in old_verify_keys.items():
                    server_key_col.maybe_update_column_width(len(key_id))

                    if old_key_data:
                        valid_until_ts_col.maybe_update_column_width(
                            len(str(old_key_data.get("expired_ts", 0)))
                        )

        # Begin constructing the message

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_key_col.pad()} | "
            f"{valid_until_ts_col.header_name}\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        total_srv_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', total_srv_line_size, pad_with='-')}\n"

        # The collection of lines to be chunked later
        list_of_result_data = []
        # Use a sorted list of server names, so it displays in alphabetical order.
        for server_name, server_response in sorted(server_to_server_data.items()):
            # There will only be data for servers that didn't time out
            first_line = True
            buffered_message = f"{server_name_col.front_pad(server_name)} | "
            if isinstance(server_response, FederationErrorResponse):
                buffered_message += f"{server_response.reason}\n"

            else:
                for server_keys in server_response.response_dict.get("server_keys", []):
                    verify_keys = server_keys.get("verify_keys", {})
                    old_verify_keys = server_keys.get("old_verify_keys", {})
                    valid_until_ts: Optional[int] = server_keys.get(
                        "valid_until_ts", None
                    )
                    valid_until_pretty = "None Found"

                    if valid_until_ts:
                        valid_until_pretty = str(
                            datetime.fromtimestamp(float(valid_until_ts / 1000))
                        )

                    if not first_line:
                        buffered_message += f"{pad('', server_name_col.size)} | "
                    for key_id in verify_keys.keys():
                        buffered_message += f"{server_key_col.pad(key_id)} | "
                        buffered_message += f"{valid_until_pretty}\n"

                    for key_id in old_verify_keys.keys():
                        expired_valid_until: Optional[int] = old_verify_keys[
                            key_id
                        ].get("expired_ts", None)
                        expired_ts_pretty = "None Found"
                        if expired_valid_until:
                            expired_ts_pretty = str(
                                datetime.fromtimestamp(
                                    float(expired_valid_until / 1000)
                                )
                            )
                        buffered_message += f"{pad('', server_name_col.size)} | "
                        buffered_message += f"{server_key_col.pad(key_id)} | "
                        buffered_message += f"{expired_ts_pretty}\n"
                    first_line = False

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                list_of_result_data.extend(
                    [f"{json.dumps(server_response.response_dict, indent=4)}\n"]
                )

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="backfill", help="Request backfill over federation for a room."
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="limit", required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def backfill_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        limit: Optional[str],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not limit:
            limit = "10"

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""
        await command_event.respond(
            f"Retrieving last {limit} Events for \n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        response = await self.federation_handler.get_backfill_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
            limit=limit,
        )

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}"
            )
            return

        # The response should contain all the pdu data inside 'pdus'
        pdu_list_from_response = response.response_dict.get("pdus", [])

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        pdu_list: List[Tuple[int, EventBase]] = []
        for event in pdu_list_from_response:
            event_base = determine_what_kind_of_event(event_id=None, data_to_use=event)
            # Don't worry about resizing the 'Extras' Column,
            # it's on the end and variable length
            dc_depth.maybe_update_column_width(len(str(event_base.depth)))
            dc_etype.maybe_update_column_width(len(event_base.event_type))
            dc_sender.maybe_update_column_width(len(event_base.sender))

            pdu_list.append((event_base.depth, event_base))

        # Sort the list in place by the first of the tuples, which is the depth
        pdu_list.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for (_, event_base) in pdu_list:
            buffered_message = ""
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()

            buffered_message += f"{line_summary}\n"

            list_of_buffer_lines.extend([buffered_message])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="event_auth", help="Request the auth chain for an event over federation"
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="server_to_request_from", required=False)
    async def event_auth_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Unlike some of the other commands, this one *requires* an event_id passed in.

        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""

        await command_event.respond(
            "Retrieving the chain of Auth Events for:\n"
            f"* Event ID: {event_id}{special_time_formatting}\n"
            f"* in Room: {room_id_or_alias or room_id}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        started_at = time.monotonic()
        response = await self.federation_handler.get_event_auth_for_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )
        total_time = time.monotonic() - started_at

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}"
            )
            return

        # The response should contain all the pdu data inside 'pdus'
        list_from_response = response.response_dict.get("auth_chain", [])
        list_of_event_bases = parse_list_response_into_list_of_event_bases(
            list_from_response
        )
        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        ordered_list: List[Tuple[int, EventBase]] = []
        for event in list_of_event_bases:
            # Don't worry about resizing the 'Extras' Column,
            # it's on the end and variable length
            dc_depth.maybe_update_column_width(len(str(event.depth)))
            dc_etype.maybe_update_column_width(len(event.event_type))
            dc_sender.maybe_update_column_width(len(event.sender))

            ordered_list.append((event.depth, event))

        # Sort the list in place by the first of the tuples, which is the depth
        ordered_list.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for (_, event_base) in ordered_list:
            buffered_message = ""
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()

            buffered_message += f"{line_summary}\n"

            list_of_buffer_lines.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_buffer_lines.extend([footer_message])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="user_devices", help="Request user devices over federation for a user."
    )
    @command.argument(name="user_mxid", parser=is_mxid, required=True)
    async def user_devices_command(
        self,
        command_event: MessageEvent,
        user_mxid: str,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        _, destination_server = user_mxid.split(":", maxsplit=1)

        await command_event.respond(
            f"Retrieving user devices for {user_mxid}\n"
            f"* From {destination_server} using {origin_server}"
        )

        response = await self.federation_handler.get_user_devices_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            user_mxid=user_mxid,
        )

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}\n\n"
                f"{json.dumps(response.response_dict, indent=4)}"
            )
            return

        await command_event.respond(
            f"```json\n{json.dumps(response.response_dict, indent=4)}\n```\n"
        )
        # # Chunk the data as there may be a few 'pages' of it
        # final_list_of_data = combine_lines_to_fit_event(
        #     list_of_buffer_lines, header_message
        # )
        #
        # for chunk in final_list_of_data:
        #     await command_event.respond(
        #         make_into_text_event(
        #             wrap_in_code_block_markdown(chunk), ignore_body=True
        #         ),
        #     )

    async def _resolve_room_id_or_alias(
        self,
        room_id_or_alias: Optional[str],
        command_event: MessageEvent,
        origin_server: str,
    ) -> Optional[str]:
        room_id = None
        if room_id_or_alias:
            # Sort out if the room id or alias passed in is valid and resolve the alias
            # to the room id if it is.
            if room_id_or_alias.startswith("#"):
                # look up the room alias. The server is extracted from the alias itself.
                alias_result = await self.federation_handler.get_room_alias_from_server(
                    origin_server=origin_server,
                    # destination_server=destination_server,
                    room_alias=room_id_or_alias,
                )
                if isinstance(alias_result, FederationErrorResponse):
                    await command_event.reply(
                        "Received an error while querying for room alias: "
                        f"{alias_result.status_code}: {alias_result.reason}"
                    )
                    # self.log.warning(f"alias_result: {alias_result}")
                    return
                else:
                    room_id = alias_result.response_dict.get("room_id")
            elif room_id_or_alias.startswith("!"):
                room_id = room_id_or_alias
            else:
                # Probably won't ever hit this, as it will be prefiltered at the command
                # invocation.
                await command_event.reply(
                    "Room ID or Alias supplied doesn't have the appropriate sigil"
                    f"(either a `!` or a `#`), '{room_id_or_alias}'"
                )
                return
        else:
            # When not supplied a room id, we assume they want the room the command was
            # issued from.
            room_id = str(command_event.room_id)
        return room_id

    async def get_hosts_in_room_ordered(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id_in_timeline: str,
    ) -> List[str]:
        # Should be a faithful recreation of what Synapse does. I see problems with this
        # though, as it doesn't calculate 'leave' events. While those servers *might*
        # still have the events that would be looked up, it's not a guarantee.
        sql = """
            SELECT
                /* Match the domain part of the MXID */
                substring(c.state_key FROM '@[^:]*:(.*)$') as server_domain
            FROM current_state_events c
            /* Get the depth of the event from the events table */
            INNER JOIN events AS e USING (event_id)
            WHERE
                /* Find any join state events in the room */
                c.type = 'm.room.member'
                AND c.membership = 'join'
                AND c.room_id = ?
            /* Group all state events from the same domain into their own buckets (groups) */
            GROUP BY server_domain
            /* Sorted by lowest depth first */
            ORDER BY min(e.depth) ASC;
        """
        # (Given the toolbox at the time of writing) I think the best way to simulate
        # this will be to use get_state_ids_from_server(), which returns a tuple of the
        # current state ids and the auth chain ids. The state ids should have all the
        # data from the room up to that point already layered to be current. Pull those
        # events, then sort them based on above.
        state_ids, _ = await self.federation_handler.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id_in_timeline,
        )
        state_events_dict = await self.federation_handler.get_events_from_server(
            origin_server, destination_server, state_ids
        )
        state_events = []
        for event_id, state_event in state_events_dict.items():
            state_events.append(state_event)
        filtered_room_member_events = cast(
            List[RoomMemberStateEvent],
            filter_events_based_on_type(state_events, "m.room.member"),
        )
        joined_member_events = cast(
            List[RoomMemberStateEvent],
            filter_state_events_based_on_membership(
                filtered_room_member_events, "join"
            ),
        )
        joined_member_events.sort(key=lambda x: x.depth)
        hosts_ordered = []
        for member in joined_member_events:
            host = get_domain_from_id(member.state_key)
            if host not in hosts_ordered:
                hosts_ordered.extend([host])

        return hosts_ordered

    async def _discover_event_ids_and_room_ids(
        self,
        origin_server: str,
        destination_server: str,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
    ) -> Optional[Tuple[str, str, int]]:
        room_id = await self._resolve_room_id_or_alias(
            room_id_or_alias, command_event, origin_server
        )
        if not room_id:
            # Don't need to actually display an error, that's handled in the above
            # function
            return

        origin_server_ts = None
        if not event_id:
            # No event id was supplied, find out what the last event in the room was
            now = int(time.time() * 1000)
            ts_response = (
                await self.federation_handler.get_timestamp_to_event_from_server(
                    origin_server=origin_server,
                    destination_server=destination_server,
                    room_id=room_id,
                    utc_time_at_ms=now,
                )
            )
            if isinstance(ts_response, FederationErrorResponse):
                await command_event.respond(
                    "Something went wrong while getting last event in room("
                    f"{ts_response.reason}"
                    "). Please supply an event_id instead at the place in time of query"
                )
                return
            else:
                event_id = ts_response.response_dict.get("event_id", None)
                origin_server_ts = ts_response.response_dict.get(
                    "origin_server_ts", None
                )
        else:
            event_result = await self.federation_handler.get_events_from_server(
                origin_server, destination_server, [event_id]
            )
            event = event_result.get(event_id, None)
            if event:
                if isinstance(event, EventError):
                    await command_event.reply(
                        "The Event ID supplied doesn't appear to be on the origin "
                        f"server({origin_server}). Try query a different server for it."
                    )
                    return

                if isinstance(event, Event):
                    room_id = event.room_id
                    origin_server_ts = event.origin_server_ts

        return room_id, event_id, origin_server_ts


def format_result_lines(
    server_name: str,
    server_name_max_size: int,
    server_software: str,
    server_software_max_size: int,
    server_version: str,
    server_version_max_size: int,
) -> str:
    buffered_message = (
        f"{pad(server_name, server_name_max_size, front=True)} | "
        f"{pad(server_software, server_software_max_size)}"
        f" | {pad(server_version, server_version_max_size, trim_backend=True)}\n"
    )
    return buffered_message


def format_result_lines_var(line_segments: List[Tuple[str, int]]) -> str:
    buffered_message = ""
    count = len(line_segments)
    for line_data, column_size in line_segments:
        pass
    return buffered_message


def wrap_in_code_block_markdown(existing_buffer: str) -> str:
    prepend_string = "```text\n"
    append_string = "```\n"
    new_buffer = ""
    if existing_buffer != "":
        new_buffer = prepend_string + existing_buffer + append_string

    return new_buffer


def make_into_text_event(
    message: str, ignore_body: bool = False
) -> TextMessageEventContent:
    content = TextMessageEventContent(
        msgtype=MessageType.NOTICE,
        body=message if not ignore_body else "no alt text available",
        format=Format.HTML,
        formatted_body=markdown.render(message),
    )

    return content


def wrap_in_pre_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<pre>\n{incoming}\n</pre>\n"
    return buffer


def wrap_in_ul_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<ul>\n{incoming}\n</ul>\n"
    return buffer


def wrap_in_li_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer += f"<li>\n{incoming}\n</li>\n"
    return buffer


def wrap_in_details(incoming: str, summary_tag: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<details>\n<summary>{summary_tag}</summary>\n"
        buffer += f"{incoming}\n</details>\n"
    return buffer


def combine_lines_to_fit_event(
    list_of_all_lines: List[str], header_line: str
) -> List[str]:
    """
    bring your own newlines

    Args:
        list_of_all_lines: strings to render(don't forget newlines)
        header_line: if you want a line at the top(description or whatever)

    Returns: List strings designed to fit into an Event's size restrictions

    """
    list_of_combined_lines = []
    # Make sure it's a copy and not a reference
    buffered_line = str(header_line)
    for line in list_of_all_lines:
        if len(buffered_line) + len(line) > MAX_EVENT_SIZE_FOR_SENDING:
            # This buffer is full, add it to the final list
            list_of_combined_lines.extend([buffered_line])
            # Don't forget to start the new buffer
            buffered_line = str(header_line)

        buffered_line += line

    # Grab the last buffer too
    list_of_combined_lines.extend([buffered_line])
    return list_of_combined_lines
