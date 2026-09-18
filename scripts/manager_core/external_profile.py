"""Explicit primary API profile selection, independent of ChatGPT credentials."""
import copy
import json


class ExternalProfile:
    def __init__(self, environment):
        raw = environment.get('CODEX_MANAGER_PRIMARY_MODEL')
        self.binding = json.loads(raw) if raw else None
        if self.binding is not None:
            if not isinstance(self.binding, dict) or not all(isinstance(self.binding.get(k), str) and self.binding[k]
                    for k in ('model', 'model_provider', 'reasoning_effort')):
                raise ValueError('External profile model binding is incomplete.')

    def request(self, message):
        if not self.binding or message.get('method') not in ('thread/start', 'thread/resume', 'thread/fork', 'turn/start', 'thread/settings/update'):
            return message
        # A shared task may remember the previous profile's GPT model. An explicit
        # API profile always runs its selected model, without changing other apps.
        result = copy.deepcopy(message)
        params = result.setdefault('params', {})
        binding = self.binding
        settings = params.get('config') or {}
        mode_settings = (params.get('collaborationMode') or {}).get('settings') or {}
        requested = (params.get('effort') or mode_settings.get('reasoning_effort') or settings.get('model_reasoning_effort'))
        supported = binding.get('supported_reasoning_efforts', [binding['reasoning_effort']])
        requested = binding.get('effort_aliases', {}).get(requested, requested)
        # A foreign task's remembered model/effort must not change this profile's
        # defaults. Once the composer uses this model, respect its explicit choice.
        effort = (requested if params.get('model') in (None, binding['model']) and requested in supported
                  else binding['reasoning_effort'])
        params['model'] = binding['model']
        if message['method'] not in ('turn/start', 'thread/settings/update'):
            params['modelProvider'] = binding['model_provider']
            config = params.setdefault('config', {}) or {}
            params['config'] = config
            config['model_provider'] = binding['model_provider']
            config['model_reasoning_effort'] = effort
            if 'context_window' in binding:
                config['model_context_window'] = binding['context_window']
                config['model_auto_compact_token_limit'] = binding['context_window'] * binding['auto_compact_percent'] // 100
        else:
            if requested is not None or message.get('params', {}).get('model') not in (None, binding['model']):
                params['effort'] = effort
            mode = params.get('collaborationMode')
            if isinstance(mode, dict):
                settings = mode.setdefault('settings', {})
                settings['model'] = binding['model']
                settings['reasoning_effort'] = effort
        return result

    def bind_auth(self, auth):
        if not self.binding:
            return auth
        process_message = auth.process
        def route_message(direction, message):
            return process_message(direction, self.request(message) if direction == 'frontend' else message)
        auth.process = route_message
        auth.state = 'ready'
        return auth
