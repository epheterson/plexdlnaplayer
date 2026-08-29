import asyncio
import unittest

import plex.plexserver as ps


class RegisterKnownDevicesTest(unittest.TestCase):
    """A renderer dropped for connection errors has to come back on its own.

    ERROR_COUNT_TO_REMOVE failures take a device out of the registry, and
    discovery only puts one back when it answers an M-SEARCH or announces
    itself. Until this watch existed, a renderer that went quiet stayed missing
    from Plex until the bridge was restarted: moving an amp from one network
    interface to another was enough to lose it for a day.
    """

    def setUp(self):
        self._devices = list(ps.devices)
        self._settings = ps.settings
        self._on_new = ps.on_new_dlna_device
        self.probed = []

        # Settings is a pydantic v1 BaseSettings, which refuses attribute
        # assignment for anything that is not a declared field, so the whole
        # object is swapped rather than the one method.
        class FakeSettings:
            urls = []

            def known_device_urls(self):
                return list(self.urls)

        ps.settings = FakeSettings()

        async def fake_on_new(url):
            self.probed.append(url)

        ps.on_new_dlna_device = fake_on_new

    def tearDown(self):
        ps.devices[:] = self._devices
        ps.settings = self._settings
        ps.on_new_dlna_device = self._on_new

    def _run(self):
        asyncio.run(ps.register_known_devices(quiet=True))

    def test_reprobes_a_device_that_dropped_out(self):
        ps.settings.urls = ["http://amp/desc.xml"]
        ps.devices[:] = []  # what remove_self leaves behind
        self._run()
        self.assertEqual(self.probed, ["http://amp/desc.xml"])

    def test_registered_device_is_never_reprobed(self):
        """Steady state must cost nothing: no request for a live renderer."""

        class Device:
            location_url = "http://amp/desc.xml"

        ps.settings.urls = ["http://amp/desc.xml"]
        ps.devices[:] = [Device()]
        self._run()
        self.assertEqual(self.probed, [])

    def test_only_the_missing_one_is_reprobed(self):
        class Device:
            location_url = "http://amp/desc.xml"

        ps.settings.urls = ["http://amp/desc.xml", "http://other/desc.xml"]
        ps.devices[:] = [Device()]
        self._run()
        self.assertEqual(self.probed, ["http://other/desc.xml"])

    def test_an_unreachable_renderer_does_not_stop_the_others(self):
        async def flaky(url):
            self.probed.append(url)
            if "dead" in url:
                raise OSError("connection refused")

        ps.on_new_dlna_device = flaky
        ps.settings.urls = ["http://dead/desc.xml", "http://amp/desc.xml"]
        ps.devices[:] = []
        self._run()
        self.assertEqual(
            sorted(self.probed), ["http://amp/desc.xml", "http://dead/desc.xml"]
        )


class WatchLoggingTest(unittest.TestCase):
    """The watch is silent while healthy, so it has to speak up on a change.

    Without this, "retrying every 60s" and "quietly broken" produce identical
    output, which is to say none.
    """

    def setUp(self):
        self._devices = list(ps.devices)
        self._settings = ps.settings
        self._register = ps.register_known_devices

        class FakeSettings:
            urls = ["http://amp/desc.xml"]

            def known_device_urls(self):
                return list(self.urls)

        ps.settings = FakeSettings()

        async def noop(quiet=False):
            return None

        ps.register_known_devices = noop

    def tearDown(self):
        ps.devices[:] = self._devices
        ps.settings = self._settings
        ps.register_known_devices = self._register

    def _sweeps(self, states, n):
        """Run the watch over a scripted sequence of registry states."""
        out = []

        class Device:
            location_url = "http://amp/desc.xml"

        seq = list(states)

        async def drive():
            task = asyncio.create_task(ps.watch_known_devices(interval=0))
            for want in seq:
                ps.devices[:] = [Device()] if want else []
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        import contextlib, io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(drive())
        out = buf.getvalue()
        return out

    def test_reports_missing_once_then_recovery(self):
        out = self._sweeps([False, False, False, True, True], 5)
        self.assertEqual(out.count("is missing, retrying"), 1)
        self.assertEqual(out.count("is back"), 1)

    def test_says_nothing_while_healthy(self):
        out = self._sweeps([True, True, True], 3)
        self.assertNotIn("is missing", out)
        self.assertNotIn("is back", out)


if __name__ == "__main__":
    unittest.main()
