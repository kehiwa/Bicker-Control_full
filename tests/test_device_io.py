import unittest

from bicker_control.device_io import (
    InputAction,
    InputChannel,
    InputService,
    MemoryGpioBackend,
    ResetAction,
    ResetButton,
    StatusLed,
)


class DeviceIoTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_debounce_and_rate_limit(self) -> None:
        gpio = MemoryGpioBackend()
        events = []
        service = InputService(
            gpio,
            (InputChannel("IN1", 5, InputAction.EVENT, debounce_seconds=0.1, rate_limit_seconds=1.0),),
            events.append,
        )

        await service.process_once(0.0)
        gpio.inputs[5] = True
        self.assertEqual(await service.process_once(0.05), ())
        emitted = await service.process_once(0.16)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].action, InputAction.EVENT)
        self.assertEqual(len(events), 1)

        gpio.inputs[5] = False
        await service.process_once(0.2)
        gpio.inputs[5] = True
        await service.process_once(0.3)
        self.assertEqual(await service.process_once(0.41), ())
        self.assertEqual(len(events), 1)

    def test_reset_action_is_selected_on_release(self) -> None:
        reset = ResetButton(network_after=3.0, factory_after=10.0)
        self.assertIsNone(reset.update(True, now=100.0))
        self.assertEqual(reset.update(False, now=102.9).action, ResetAction.NONE)

        reset.update(True, now=200.0)
        network = reset.update(False, now=203.0)
        self.assertIsNotNone(network)
        self.assertEqual(network.action, ResetAction.NETWORK)

        reset.update(True, now=300.0)
        factory = reset.update(False, now=310.0)
        self.assertIsNotNone(factory)
        self.assertEqual(factory.action, ResetAction.FACTORY)

    def test_status_led_maps_named_states(self) -> None:
        gpio = MemoryGpioBackend()
        led = StatusLed(gpio, 22, 23, 24)
        led.set_state("ready")
        self.assertEqual(gpio.outputs, {22: False, 23: True, 24: False})
        with self.assertRaises(ValueError):
            led.set_state("unknown")


if __name__ == "__main__":
    unittest.main()