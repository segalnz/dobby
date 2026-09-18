from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch
import config
from command_router import CommandRouter

class ActionTests(unittest.TestCase):
    def router(self):
        router = CommandRouter.__new__(CommandRouter)
        router._deferred_action = None
        router.device_registry = {'lamp': {'actions': {'on': {'topic':'test','payload':'on'}}}}
        router._device_alias_map = {'lamp':'lamp'}
        router._sensor_alias_map = {}
        router._fast_parse = Mock(return_value={'intent':'device_control','confidence':1,'device':'lamp','action':'on'})
        router._dobby_say = Mock(return_value='Dobby obeys, sir.')
        router._safe_publish = Mock(return_value=True)
        return router

    def test_both_acknowledgment_orders_and_failure(self):
        for order in ('before','after'):
            with self.subTest(order=order), patch.object(config,'DEVICE_ACKNOWLEDGMENT_ORDER',order), patch('jellyfin_music.get_music_controller') as music:
                music.return_value.is_music_command.return_value = False
                router = self.router()
                router._safe_publish.return_value = False
                result = router.handle_text('turn lamp on')
                if order == 'before':
                    router._safe_publish.assert_not_called()
                    self.assertEqual(result.response_text, 'Dobby obeys, sir.')
                    self.assertIn('could not confirm', router.execute_deferred_action())
                    self.assertIsNone(router.execute_deferred_action())
                    router._safe_publish.assert_called_once()
                else:
                    router._safe_publish.assert_called_once_with('lamp','on',None)
                    self.assertIn('could not confirm', result.response_text)
                    self.assertIsNone(router.execute_deferred_action())

    def test_mqtt_timeout_is_not_success(self):
        router = self.router()
        router._mqtt_connected = True
        router._mqtt = Mock()
        info = router._mqtt.publish.return_value
        info.rc = 0
        info.is_published.return_value = False
        self.assertFalse(router._publish_device_action('lamp','on'))
        info.wait_for_publish.assert_called_once()

    def test_broker_reconnect_restores_subscriptions(self):
        router = self.router()
        router.automations = Mock()
        router.automations.pending.return_value = [{'topic':'sensor/temperature'}, {'due':1}]
        client = Mock()
        router._on_connect(client, None, {}, 0)
        client.subscribe.assert_called_once_with('sensor/temperature')
