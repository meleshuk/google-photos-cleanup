import unittest
from types import SimpleNamespace

from gphotos_cleanup.finder_evidence import resolve_duplicate_membership
from gphotos_cleanup.finder_inventory import FileRec
from gphotos_cleanup.finder_matching import group_items, group_library_items
from gphotos_cleanup.finder_sources import extract_upload_mechanism


class HashSource:
    def __init__(self, results):
        self.results = results

    def hash_media(self, entry):
        result = self.results[entry.name]
        if isinstance(result, Exception):
            raise result
        return result


class EvidenceFailureTests(unittest.TestCase):
    def records(self, results):
        source = HashSource(results)

        def record(name, scope):
            entry = SimpleNamespace(name=name, relpath=scope + '/' + name,
                                    size=10, locator='directory-file', source=source)
            metadata = {'title': 'IMG_1.JPG', 'photoTakenTime': {'timestamp': '123'},
                        'url': 'https://photos.google.com/photo/AF1QipFAKE' + name,
                        'googlePhotosOrigin': {'webUpload': {}}}
            return FileRec(entry.relpath, name, scope, scope, name + '.json',
                           metadata, 'exact', 'fake-source', entry=entry)

        library = group_library_items([record('A.JPG', 'library'), record('B.JPG', 'library')])
        album = group_items([record('ALBUM.JPG', 'album')])
        return library, album

    def test_unreadable_duplicate_remains_a_possible_album_member(self):
        library, album = self.records({'A.JPG': OSError('unreadable'),
                                       'B.JPG': 'matching-digest',
                                       'ALBUM.JPG': 'matching-digest'})
        decisions, warnings = resolve_duplicate_membership(library, album, hash_media=True)
        self.assertEqual({row['verdict'] for row in decisions.values()}, {'uncertain'})
        self.assertTrue(warnings)

    def test_unreadable_album_file_cannot_release_duplicates(self):
        library, album = self.records({'A.JPG': 'a', 'B.JPG': 'b',
                                       'ALBUM.JPG': OSError('unreadable')})
        decisions, warnings = resolve_duplicate_membership(library, album, hash_media=True)
        self.assertEqual({row['verdict'] for row in decisions.values()}, {'uncertain'})
        self.assertTrue(warnings)

    def test_unrecognized_origin_never_becomes_matching_evidence(self):
        for origin in ({'futureUpload': {}}, {'webUpload': []},
                       {'webUpload': {}, 'mobileUpload': {}}, None):
            with self.subTest(origin=origin):
                self.assertIsNone(extract_upload_mechanism({'googlePhotosOrigin': origin}))


if __name__ == '__main__':
    unittest.main()
