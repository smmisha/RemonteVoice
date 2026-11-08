import unittest
from unittest.mock import patch

from remotevoice.input_routing import InputRoute, InputRoutingError


class InputRoutingTests(unittest.TestCase):
    def route(self, defaults=None):
        self.defaults = dict(defaults or {0: "laptop-mic", 1: "headset", 2: "laptop-mic"})
        self.calls = []

        def set_default(endpoint, role):
            self.calls.append((endpoint, role))
            self.defaults[role] = endpoint

        return InputRoute("phone-mic", "laptop-mic", get_default=self.defaults.__getitem__,
                          set_default=set_default)

    def test_connect_and_disconnect_route_all_three_capture_roles(self):
        route = self.route()
        route.activate()
        self.assertEqual(self.defaults, {0: "phone-mic", 1: "phone-mic", 2: "phone-mic"})
        self.assertEqual(route.restore(), 3)
        self.assertEqual(self.defaults, {0: "laptop-mic", 1: "laptop-mic", 2: "laptop-mic"})
        self.assertEqual(route.restore(), 0)

    def test_restore_preserves_manual_input_selection(self):
        route = self.route({0: "phone-mic", 1: "phone-mic", 2: "user-headset"})
        self.assertEqual(route.restore(), 2)
        self.assertEqual(self.defaults[2], "user-headset")

    def test_repeated_activation_does_not_repeat_changes(self):
        route = self.route()
        route.activate()
        route.activate()
        self.assertEqual(len(self.calls), 3)

    def test_partial_activation_restores_snapshot_even_when_set_changes_then_raises(self):
        route = self.route()
        original = dict(self.defaults)
        ordinary_set = route._set

        def failing_set(endpoint, role):
            ordinary_set(endpoint, role)
            if endpoint == "phone-mic" and role == 1:
                raise OSError("test partial mutation")

        route._set = failing_set
        with self.assertRaisesRegex(InputRoutingError, "микрофон на телефон"):
            route.activate()
        self.assertEqual(self.defaults, original)

    def test_snapshot_failure_does_not_mutate_input(self):
        route = self.route()

        def failing_get(role):
            if role == 1:
                raise OSError("test read failure")
            return self.defaults[role]

        route._get = failing_get
        with self.assertRaises(InputRoutingError):
            route.activate()
        self.assertEqual(self.calls, [])

    def test_restore_continues_other_roles_when_one_fails(self):
        route = self.route({0: "phone-mic", 1: "phone-mic", 2: "phone-mic"})
        ordinary_set = route._set

        def failing_set(endpoint, role):
            if role == 0:
                raise OSError("test disabled physical mic")
            ordinary_set(endpoint, role)

        route._set = failing_set
        with self.assertRaisesRegex(InputRoutingError, "микрофона на ноутбук"):
            route.restore()
        self.assertEqual(self.defaults, {0: "phone-mic", 1: "laptop-mic", 2: "laptop-mic"})

    def test_silent_native_noop_is_failure(self):
        route = self.route({0: "phone-mic", 1: "phone-mic", 2: "phone-mic"})
        route._set = lambda endpoint, role: None
        with self.assertRaises(InputRoutingError):
            route.restore()

    def test_default_dependencies_are_microphone_specific(self):
        with patch("remotevoice.input_routing.get_default_microphone") as getter, patch(
            "remotevoice.input_routing.set_default_microphone"
        ) as setter:
            route = InputRoute("phone-mic", "laptop-mic")
            self.assertIs(route._get, getter)
            self.assertIs(route._set, setter)

    def test_invalid_endpoint_choices_are_rejected(self):
        for remote, fallback in (("", "laptop-mic"), ("phone-mic", ""), ("same", "same")):
            with self.subTest(remote=remote, fallback=fallback), self.assertRaises(ValueError):
                InputRoute(remote, fallback)


if __name__ == "__main__":
    unittest.main()
