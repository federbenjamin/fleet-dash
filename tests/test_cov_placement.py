"""Coverage for fleetdash.placement edge branches."""
import time
import unittest

from fleetdash.placement import (classify_placement, completed_handoff,
                                  requests_reply)


class PlacementEdgeTests(unittest.TestCase):
    def test_requests_reply_empty_is_false(self):
        self.assertFalse(requests_reply(""))
        self.assertFalse(requests_reply(None))
        self.assertFalse(requests_reply("   "))

    def test_completed_handoff_empty_is_false(self):
        self.assertFalse(completed_handoff(""))
        self.assertFalse(completed_handoff(None))

    def test_stalled_or_prompt_state_goes_to_needs_you(self):
        session = {"session_id": "s", "state": "stalled_or_prompt",
                   "convo_v": "1", "quiet_s": 5}
        placement = classify_placement(session, time.time())
        self.assertEqual(placement["ui_group"], "needs_you")
        self.assertEqual(placement["reason_label"], "Check session")
        self.assertEqual(placement["winning_rule"],
                         "placement.state.stalled_or_prompt")


if __name__ == "__main__":
    unittest.main()
