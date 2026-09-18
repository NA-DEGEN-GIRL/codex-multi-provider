"""Observe complete runtime events while preserving the frontend's opt-outs."""
from copy import deepcopy


class NotificationPolicy:
    def __init__(self):
        self.omitted = frozenset()
        self.initialized = False

    def to_runtime(self, message):
        if message.get('method') != 'initialize' or self.initialized:
            return message
        result = deepcopy(message)
        params = result.setdefault('params', {})
        if not isinstance(params, dict):
            raise ValueError('Invalid initialize parameters.')
        capabilities = params.setdefault('capabilities', {})
        if capabilities is None:
            capabilities = params['capabilities'] = {}
        if not isinstance(capabilities, dict):
            raise ValueError('Invalid initialize capabilities.')
        omitted = capabilities.get('optOutNotificationMethods')
        if omitted is None:
            omitted = []
        if (not isinstance(omitted, list) or len(omitted) > 256
                or any(not isinstance(item, str) or len(item) > 160 for item in omitted)):
            raise ValueError('Invalid notification preference.')
        self.omitted = frozenset(omitted)
        capabilities['optOutNotificationMethods'] = []
        capabilities['experimentalApi'] = True
        self.initialized = True
        return result

    def to_frontend(self, message):
        # Never suppress an RPC request (including approvals) or response.
        return 'id' in message or message.get('method') not in self.omitted
