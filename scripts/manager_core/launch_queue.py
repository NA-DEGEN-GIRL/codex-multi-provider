"""Serialize launch preparation, allowing a selected waiting profile to go next."""
import threading


class LaunchQueue:
    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = []
        self.owner = None
        self.depth = 0
        self.preferred = None

    def prefer(self, profile_id):
        with self.condition:
            self.preferred = profile_id
            self.condition.notify_all()

    def acquire(self, profile_id=None):
        owner = threading.get_ident()
        with self.condition:
            if self.owner == owner:
                self.depth += 1
                return
            ticket = (object(), profile_id)
            self.waiting.append(ticket)
            self.condition.notify_all()
            try:
                while True:
                    # A maintenance request remains a fence: do not move a
                    # selected launch ahead of already queued maintenance.
                    fence = next((i for i, row in enumerate(self.waiting) if row[1] is None), len(self.waiting))
                    candidates = self.waiting[:fence]
                    next_ticket = next((row for row in candidates if row[1] == self.preferred), self.waiting[0])
                    if self.owner is None and next_ticket is ticket:
                        break
                    self.condition.wait()
                self.owner, self.depth = owner, 1
                if self.preferred == profile_id:
                    self.preferred = None
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()

    def release(self):
        with self.condition:
            if self.owner != threading.get_ident():
                raise RuntimeError('Launch queue release by a different worker.')
            self.depth -= 1
            if self.depth == 0:
                self.owner = None
                self.condition.notify_all()
