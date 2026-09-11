"""WS transport protocol class selected by the container CMD via `--ws`.

c354: uvicorn's stock websockets-sansio adapter (websockets_sansio_impl.py,
handle_cont) appends every nonfinal continuation frame to `self.frames` with no
count bound -- only the joined byte total is checked, and only once FIN arrives
(send_receive_event_to_app). A sender that starts a message (fin=False) and then
emits continuation frames that never carry FIN can grow that list without limit
while every individual frame and the running byte total stay under
--ws-max-size. See WEBSOCKET-RESOURCE-LIMITS.md.

BoundedFragmentWebSocketsProtocol closes that gap at the same layer, by
overriding only handle_cont to count retained fragments and fail the connection
once WS_MAX_CONTINUATION_FRAMES is reached -- reusing the same close path the
base class already uses one method away for a different malformed-input case
(send_receive_event_to_app's UnicodeDecodeError branch: conn.send_close(1007)
then handle_parser_exception()).

No __init__ override: uvicorn's existing call sites construct this class the
same way they construct the stock one, so it is a drop-in `--ws` value.
"""
from __future__ import annotations

from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from websockets.frames import Frame

# Order of magnitude of app/ws/gateway.py's outbound WS_QUEUE_MAX_FRAMES=32 (a
# plain module constant there too, not a Settings field -- same convention).
# The app currently has no legitimate inbound application message at all
# (ping/pong only, per WEBSOCKET-RESOURCE-LIMITS.md), so this is a generous but
# finite ceiling closing an unbounded gap rather than a tuned limit protecting a
# known use case.
WS_MAX_CONTINUATION_FRAMES = 64


class BoundedFragmentWebSocketsProtocol(WebSocketsSansIOProtocol):
    """WebSocketsSansIOProtocol with a bound on retained continuation fragments.

    Checks len(self.frames) BEFORE appending, so self.frames itself never grows
    past WS_MAX_CONTINUATION_FRAMES entries -- the list stays bounded, not just
    the eventual close.
    """

    def handle_cont(self, event: Frame) -> None:
        # Once the bound trips once, handle_parser_exception() has already
        # moved self.conn (the websockets ServerProtocol) to CLOSING and set
        # self.close_sent True -- but nothing stops another CONT frame from
        # reaching handle_cont after that: either several buffered frames in
        # the SAME data_received() batch (handle_events() dispatches all of
        # them before returning), or a peer that keeps sending across further
        # separate reads once past-bound. Either way, without this guard
        # len(self.frames) is still >= the bound, so the branch below runs
        # again and calls conn.send_close() on a connection already CLOSING,
        # which raises websockets.exceptions.InvalidState -- runtime-verified
        # via a raw-socket client during manual proof, and caught by
        # test_frames_after_close_do_not_keep_growing's sabotage matrix.
        if self.close_sent:
            return
        if len(self.frames) >= WS_MAX_CONTINUATION_FRAMES:
            self.conn.send_close(1009)
            self.handle_parser_exception()
            return
        super().handle_cont(event)


# Module-level surface assertion (chirps-17 condition 3): the class-level shapes
# this override relies on must still exist on the installed base class. Runs at
# import time so a base-class change that removes/renames either method fails
# loudly the first time anything imports this module, not silently at a random
# runtime close. The instance-level shapes (self.frames, self.conn.send_close,
# self.conn.close_sent) can only be checked against a live connection; that half
# of the surface is asserted in tests/test_c354_inbound_fragment_bound.py::
# test_base_class_surface_matches_what_the_override_relies_on.
assert callable(getattr(WebSocketsSansIOProtocol, "handle_cont", None)), (
    "WebSocketsSansIOProtocol no longer defines handle_cont -- BoundedFragmentWebSocketsProtocol's override is stale"
)
assert callable(getattr(WebSocketsSansIOProtocol, "handle_parser_exception", None)), (
    "WebSocketsSansIOProtocol no longer defines handle_parser_exception -- the close path this override reuses is gone"
)
