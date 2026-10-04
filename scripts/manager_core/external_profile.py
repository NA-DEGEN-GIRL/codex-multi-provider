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
        if self.binding.get('agent_kind') == 'claude_code':
            return self._claude_request(message)
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

    def _claude_request(self, message):
        from .claude_profiles import MODEL_EFFORTS, automatic_context_window

        result = copy.deepcopy(message)
        params = result.setdefault('params', {})
        binding = self.binding
        sparse = message['method'] in ('turn/start', 'thread/settings/update')
        config = params.get('config') or {}
        mode = params.get('collaborationMode')
        mode_settings = (mode or {}).get('settings') or {}
        # Native collaboration settings take precedence over the legacy fields.
        requested_model = mode_settings.get('model') or params.get('model') or config.get('model')
        allowed = {'cc-' + model: efforts for model, efforts in MODEL_EFFORTS.items()}
        advertised = binding.get('supported_models')
        if isinstance(advertised, dict):
            allowed = {model: tuple(effort for effort in efforts if effort in advertised[model])
                       for model, efforts in allowed.items()
                       if isinstance(advertised.get(model), list)}
            allowed = {model: efforts for model, efforts in allowed.items() if efforts}
        if binding['model'] not in allowed or not allowed[binding['model']]:
            raise ValueError('Claude profile model catalog is incomplete.')
        approved_model = isinstance(requested_model, str) and requested_model in allowed
        model = requested_model if approved_model else binding['model']
        requested = (mode_settings.get('reasoning_effort') or params.get('effort')
                     or config.get('model_reasoning_effort'))
        requested = binding.get('effort_aliases', {}).get(requested, requested)
        supported = allowed[model]
        default = binding['reasoning_effort'] if binding['reasoning_effort'] in supported else (
            'high' if 'high' in supported else supported[0])
        effort = (requested if (requested_model is None or approved_model) and requested in supported else default)
        if not sparse or requested_model is not None:
            params['model'] = model
        if sparse:
            # An omitted model/effort retains the task's selected values, even
            # after another task used a different model in the same profile.
            if requested is not None or (requested_model is not None and not approved_model):
                params['effort'] = effort
            if isinstance(mode, dict):
                settings = mode.setdefault('settings', {})
                if requested_model is not None:
                    settings['model'] = model
                if requested is not None or (requested_model is not None and not approved_model):
                    settings['reasoning_effort'] = effort
            return result

        params['modelProvider'] = binding['model_provider']
        params['config'] = config
        if 'model' in config:
            config['model'] = model
        config['model_provider'] = binding['model_provider']
        config['model_reasoning_effort'] = effort
        window = binding.get('context_window')
        percent = binding.get('auto_compact_percent')
        if window is None:
            config.pop('model_context_window', None)
        else:
            config['model_context_window'] = window
        if percent is None:
            config.pop('model_auto_compact_token_limit', None)
        else:
            context = window or automatic_context_window(model)
            config['model_auto_compact_token_limit'] = context * percent // 100
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
