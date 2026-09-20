import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import catalog_frames, note_aliases


class RemoteNoteAliasesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = dict(host_id='remote-ssh-discovered:fixture', thread_id=str(uuid.uuid4()))
        self.directory = self.root / 'work/control-center'
        self.cache = self.directory / 'catalog/ssh' / (hashlib.sha256(b'fixture').hexdigest() + '.jsonl')
        self.rows = []

    def old_note(self, source):
        task = dict(host_id=self.task['host_id'], thread_id=str(uuid.uuid5(note_aliases._NAMESPACE,
                    f"local\0{source}\0{self.task['thread_id']}")))
        path = note_aliases.document_path(self.root, task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(version=3, task=task, notes=['unchanged image note'])))
        self.rows.append(dict(thread_id=self.task['thread_id'], source_store_id=source))
        catalog_frames.atomic_write(self.cache, dict(conversations=self.rows))
        return task, path

    def test_existing_projection_note_is_reused_without_copying_or_modification(self):
        previous, path = self.old_note('legacy:fixture')
        original = path.read_bytes()
        note_aliases.refresh(self.root, self.task)
        aliases = json.loads((self.directory / 'note-aliases.json').read_text())
        self.assertEqual(aliases, {self.task['host_id']+'\0'+self.task['thread_id']: previous['thread_id']})
        note_aliases.refresh(self.root, self.task)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(note_aliases.document_path(self.root, self.task).exists())

    def test_canonical_notes_are_never_replaced_by_old_notes(self):
        self.old_note('legacy:fixture')
        path = note_aliases.document_path(self.root, self.task)
        path.write_text('existing canonical notes')
        note_aliases.refresh(self.root, self.task)
        self.assertEqual(path.read_text(), 'existing canonical notes')
        self.assertFalse((self.directory / 'note-aliases.json').exists())

    def test_two_existing_note_documents_do_not_silently_hide_one(self):
        _, first = self.old_note('legacy:first')
        _, second = self.old_note('legacy:second')
        before = [first.read_bytes(), second.read_bytes()]
        with self.assertRaisesRegex(ValueError, 'Several'):
            note_aliases.refresh(self.root, self.task)
        self.assertEqual(before, [first.read_bytes(), second.read_bytes()])
        self.assertFalse((self.directory / 'note-aliases.json').exists())

    def test_other_host_notes_are_not_linked(self):
        self.old_note('legacy:fixture')
        task = dict(self.task, host_id='ssh:other')
        note_aliases.refresh(self.root, task)
        self.assertFalse((self.directory / 'note-aliases.json').exists())


if __name__ == '__main__':
    unittest.main()
