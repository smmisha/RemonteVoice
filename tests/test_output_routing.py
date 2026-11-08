import unittest

from remotevoice.output_routing import OutputRoute, OutputRoutingError


class OutputRoutingTests(unittest.TestCase):
    def route(self, defaults=None):
        self.defaults = dict(defaults or {0: "speakers", 1: "headphones", 2: "speakers"})
        self.calls = []

        def set_default(endpoint, role):
            self.calls.append((endpoint, role))
            self.defaults[role] = endpoint

        return OutputRoute("remote", "speakers", get_default=self.defaults.__getitem__,
                           set_default=set_default)

    def test_connect_routes_every_role_and_disconnect_restores_physical_output(self):
        route = self.route()
        route.activate()
        self.assertEqual(self.defaults, {0: "remote", 1: "remote", 2: "remote"})
        self.assertEqual(route.restore(), 3)
        self.assertEqual(self.defaults, {0: "speakers", 1: "speakers", 2: "speakers"})
        self.assertEqual(route.restore(), 0)

    def test_restore_preserves_manual_change_to_another_output(self):
        route = self.route({0: "remote", 1: "remote", 2: "new-headphones"})
        self.assertEqual(route.restore(), 2)
        self.assertEqual(self.defaults[2], "new-headphones")

    def test_repeated_connected_status_does_not_repeat_switch(self):
        route = self.route()
        route.activate()
        route.activate()
        self.assertEqual(len(self.calls), 3)

    def test_partial_activation_rolls_back_including_a_setter_that_changes_then_raises(self):
        route = self.route()
        original = dict(self.defaults)
        ordinary_set = route._set

        def failing_set(endpoint, role):
            ordinary_set(endpoint, role)
            if endpoint == "remote" and role == 1:
                raise OSError("simulated native failure after side effect")

        route._set = failing_set
        with self.assertRaises(OutputRoutingError):
            route.activate()
        self.assertEqual(self.defaults, original)

    def test_snapshot_failure_makes_no_mutation(self):
        route = self.route()

        def failing_get(role):
            if role == 1:
                raise OSError("simulated COM failure")
            return self.defaults[role]

        route._get = failing_get
        with self.assertRaises(OutputRoutingError):
            route.activate()
        self.assertEqual(self.calls, [])

    def test_restore_continues_other_roles_and_reports_failure(self):
        route = self.route({0: "remote", 1: "remote", 2: "remote"})
        ordinary_set = route._set

        def failing_set(endpoint, role):
            if role == 0:
                raise OSError("simulated disabled endpoint")
            ordinary_set(endpoint, role)

        route._set = failing_set
        with self.assertRaises(OutputRoutingError):
            route.restore()
        self.assertEqual(self.defaults, {0: "remote", 1: "speakers", 2: "speakers"})

    def test_silent_native_noop_is_not_reported_as_restored(self):
        route = self.route({0: "remote", 1: "remote", 2: "remote"})
        route._set = lambda endpoint, role: None
        with self.assertRaises(OutputRoutingError):
            route.restore()

    def test_invalid_endpoint_choices_are_rejected(self):
        for remote, fallback in (("", "speakers"), ("remote", ""), ("same", "same")):
            with self.subTest(remote=remote, fallback=fallback), self.assertRaises(ValueError):
                OutputRoute(remote, fallback)


if __name__ == "__main__":
    unittest.main()
