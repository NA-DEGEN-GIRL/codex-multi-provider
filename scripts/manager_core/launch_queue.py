"""Admit different profiles' launches together; maintenance and a profile run alone.

A launch (``acquire(profile_id)``) shares admission with other profiles'
launches and waits only for another worker holding the same profile.
Maintenance (``acquire(None)``) or an exclusive launch waits for every holder
and remains a fence: a later request, even a selected one, waits until it has
finished. A holder re-entering never waits for a fence it is part of.

A selected profile (``prefer``) goes ahead of launches queued before it, never
ahead of queued maintenance. Different profiles are admitted together anyway;
this matters while launches are exclusive (the pending one-time migration),
when a clicked profile must not wait behind queued warmup launches.
"""
import threading


class LaunchQueue:
    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = []
        # Worker thread -> stack of (profile_id, exclusive) it holds.
        self.held = {}
        self.preferred = None

    @property
    def depth(self):
        """How often the calling worker currently holds admission."""
        with self.condition:
            return len(self.held.get(threading.get_ident(), ()))

    def holding(self):
        return self.depth > 0

    def prefer(self, profile_id):
        with self.condition:
            self.preferred = profile_id
            self.condition.notify_all()

    def _order(self):
        """Waiting tickets in admission order: the selected launch first."""
        for index, row in enumerate(self.waiting):
            if row[1] is None:
                break  # Queued maintenance remains a fence.
            if row[1] == self.preferred:
                return [row, *self.waiting[:index], *self.waiting[index + 1:]]
        return self.waiting

    def _held_elsewhere(self, profile_id, owner):
        return any(any(held == profile_id for held, _ in stack)
                   for worker, stack in self.held.items() if worker != owner)

    def _exclusive_held(self):
        return any(exclusive for stack in self.held.values() for _, exclusive in stack)

    def _admissible(self, ticket):
        _, profile_id, exclusive, owner = ticket
        order = self._order()
        ahead = order[:order.index(ticket)]
        if exclusive:
            return not self.held and not ahead
        return (not self._exclusive_held()
                and not any(row[2] for row in ahead)
                and not any(row[1] == profile_id for row in ahead)
                and not self._held_elsewhere(profile_id, owner))

    def acquire(self, profile_id=None, *, exclusive=None):
        owner = threading.get_ident()
        exclusive = profile_id is None if exclusive is None else bool(exclusive)
        with self.condition:
            stack = self.held.get(owner)
            if stack is not None:
                if exclusive and not any(row[1] for row in stack):
                    # Waiting for every holder would include this worker.
                    raise RuntimeError('A launch cannot become exclusive while it is admitted.')
                if not any(row[1] for row in stack) and profile_id is not None:
                    # Nested opens (restore, SSH, conversation) never wait for
                    # queued maintenance, only for another launch of this profile.
                    self.condition.wait_for(lambda: not self._held_elsewhere(profile_id, owner))
                stack.append((profile_id, exclusive))
                return
            ticket = (object(), profile_id, exclusive, owner)
            self.waiting.append(ticket)
            self.condition.notify_all()
            try:
                self.condition.wait_for(lambda: self._admissible(ticket))
                self.held[owner] = [(profile_id, exclusive)]
                if profile_id is not None and self.preferred == profile_id:
                    self.preferred = None
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()

    def release(self):
        owner = threading.get_ident()
        with self.condition:
            stack = self.held.get(owner)
            if not stack:
                raise RuntimeError('Launch queue release by a different worker.')
            stack.pop()
            if not stack:
                del self.held[owner]
            self.condition.notify_all()
