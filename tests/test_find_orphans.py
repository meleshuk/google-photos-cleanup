import csv
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINDER = ROOT / "gphotos_find_orphans.py"


def sidecar(title, timestamp, suffix):
    return json.dumps({
        "title": title,
        "photoTakenTime": {"timestamp": str(timestamp)},
        "url": f"https://photos.google.com/photo/AF1QipFAKE{suffix}",
    })


class FinderIntegrationTests(unittest.TestCase):
    def run_export(self, entries):
        """Exercise the whole source/pairing/classification/reporting pipeline."""
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "takeout-20260101T000000Z-1-001.zip"
            output = directory / "orphans.json"
            with zipfile.ZipFile(archive, "w") as takeout:
                for name, content in entries.items():
                    takeout.writestr(f"Takeout/Google Photos/{name}", content)
            result = subprocess.run(
                [sys.executable, str(FINDER), str(archive), "-o", str(output)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(report["counts"]["reconciles"])
            self.assertEqual(report["counts"]["stem_guard_crosscheck_violations"], 0)
            # The same row set must reach the CSV consumed by the deleter.
            with output.with_suffix(".csv").open(newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual([row["url"] for row in csv_rows],
                             [item["url"] for item in report["orphans"]])
            return report

    def duplicate_entries(self, second_name="IMG_1194.JPG"):
        first = "Photos from 2020/IMG_1194.HEIC"
        second = "Photos from 2020/" + second_name
        return {
            first: b"first still",
            first + ".json": sidecar("IMG_1194.HEIC", 1599940534, "FIRST"),
            second: b"second still",
            second + ".json": sidecar("IMG_1194.JPG", 1599940534, "SECOND"),
        }

    def test_jpg_heic_duplicate_stills_get_separate_orphan_addresses(self):
        report = self.run_export(self.duplicate_entries())
        self.assertEqual(report["counts"]["library_logical_items"], 2)
        self.assertEqual(report["counts"]["orphans"], 2)
        self.assertEqual({item["url"] for item in report["orphans"]}, {
            "https://photos.google.com/photo/AF1QipFAKEFIRST",
            "https://photos.google.com/photo/AF1QipFAKESECOND",
        })
        self.assertEqual({item["title"] for item in report["orphans"]},
                         {"IMG_1194.HEIC", "IMG_1194.JPG"})
        self.assertTrue(all(item["file_count"] == 1 for item in report["orphans"]))
        self.assertEqual(sum(run["items"] for run in report["contiguous_runs"]), 2)

    def test_identical_title_duplicates_get_separate_orphan_addresses(self):
        entries = self.duplicate_entries("IMG_1194(1).HEIC")
        entries["Photos from 2020/IMG_1194(1).HEIC.json"] = sidecar(
            "IMG_1194.HEIC", 1599940534, "SECOND")
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 2)
        self.assertEqual(len({item["url"] for item in report["orphans"]}), 2)

    def test_duplicate_album_proof_withholds_both_stills(self):
        entries = self.duplicate_entries()
        entries["My Album/IMG_1194.JPG.json"] = sidecar(
            "IMG_1194.JPG", 1599940534, "ALBUMCOPY")
        report = self.run_export(entries)
        self.assertEqual(report["orphans"], [])
        self.assertEqual(report["counts"]["uncertain"], 2)
        self.assertTrue(all("which duplicate" in item["reason"]
                            for item in report["uncertain"]))

    def test_one_url_transcode_renditions_stay_one_item(self):
        entries = self.duplicate_entries()
        entries["Photos from 2020/IMG_1194.JPG.json"] = sidecar(
            "IMG_1194.HEIC", 1599940534, "FIRST")
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 1)
        self.assertEqual(report["orphans"][0]["file_count"], 2)

    def test_folded_motion_companion_stays_attached_to_base_url(self):
        entries = {
            "Photos from 2020/IMG_1194.HEIC": b"still",
            "Photos from 2020/IMG_1194.HEIC.json": sidecar(
                "IMG_1194.HEIC", 1599940534, "LIVE"),
            "Photos from 2020/IMG_1194.MOV": b"motion",
        }
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 1)
        self.assertTrue(report["orphans"][0]["live_photo_pair"])
        self.assertEqual(report["orphans"][0]["file_count"], 2)

    def keyed_motion_entries(self, video_creation=1600000000):
        entries = {}
        for name, suffix, created in (
            ("IMG_1194.HEIC", "STILL", 1600000000),
            ("IMG_1194.MOV", "MOTION", video_creation),
        ):
            metadata = json.loads(sidecar(name, 1599940534, suffix))
            metadata["creationTime"] = {"timestamp": str(created)}
            entries["Photos from 2020/" + name] = b"media"
            entries["Photos from 2020/" + name + ".json"] = json.dumps(metadata)
        return entries

    def test_keyed_motion_companions_need_matching_upload_and_filename(self):
        report = self.run_export(self.keyed_motion_entries())
        self.assertEqual(report["counts"]["orphans"], 1)
        item = report["orphans"][0]
        self.assertTrue(item["live_photo_pair"])
        self.assertTrue(item["url"].endswith("STILL"))

    def test_separate_uploads_of_still_and_video_are_uncertain(self):
        report = self.run_export(self.keyed_motion_entries(video_creation=1600000010))
        self.assertEqual(report["orphans"], [])
        self.assertEqual(report["counts"]["uncertain"], 2)

    def test_motion_cannot_choose_between_two_still_duplicates(self):
        entries = self.keyed_motion_entries()
        entries["Photos from 2020/IMG_1194.JPG"] = b"another still"
        metadata = json.loads(sidecar("IMG_1194.JPG", 1599940534, "OTHER"))
        metadata["creationTime"] = {"timestamp": "1600000000"}
        entries["Photos from 2020/IMG_1194.JPG.json"] = json.dumps(metadata)
        report = self.run_export(entries)
        self.assertEqual(report["orphans"], [])
        self.assertEqual(report["counts"]["uncertain"], 3)

    def test_numeric_dedup_suffix_prevents_false_motion_pair(self):
        entries = self.keyed_motion_entries()
        entries["Photos from 2020/IMG_1194(1).MOV"] = entries.pop(
            "Photos from 2020/IMG_1194.MOV")
        entries["Photos from 2020/IMG_1194(1).MOV.json"] = entries.pop(
            "Photos from 2020/IMG_1194.MOV.json")
        report = self.run_export(entries)
        self.assertEqual(report["orphans"], [])
        self.assertEqual(report["counts"]["uncertain"], 2)

    def test_resolved_motion_pair_survives_a_separate_still_duplicate(self):
        entries = self.keyed_motion_entries()
        entries["Photos from 2020/IMG_1194(1).HEIC"] = b"another still"
        metadata = json.loads(sidecar("IMG_1194.HEIC", 1599940534, "OTHER"))
        metadata["creationTime"] = {"timestamp": "1600000000"}
        entries["Photos from 2020/IMG_1194(1).HEIC.json"] = json.dumps(metadata)
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 2)
        self.assertEqual(sum(item["live_photo_pair"] for item in report["orphans"]), 1)

    def test_sidecar_only_duplicate_never_borrows_others_media(self):
        entries = self.duplicate_entries()
        del entries["Photos from 2020/IMG_1194.JPG"]
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 1)
        self.assertEqual(report["counts"]["uncertain"], 1)
        self.assertTrue(report["orphans"][0]["url"].endswith("FIRST"))
        self.assertTrue(report["uncertain"][0]["url"].endswith("SECOND"))

    def test_range_accounting_does_not_include_an_uncertain_twin(self):
        entries = self.duplicate_entries()
        # Same-extension timestamps two minutes apart trigger the wider guard;
        # the HEIC/JPG mismatch is beyond the tighter 60-second guard.
        entries["My Album/IMG_1194.JPG.json"] = sidecar(
            "IMG_1194.JPG", 1599940654, "NEARBYALBUM")
        report = self.run_export(entries)
        self.assertEqual(report["counts"]["orphans"], 1)
        self.assertEqual(report["counts"]["uncertain"], 1)
        self.assertEqual(sum(run["items"] for run in report["contiguous_runs"]), 1)
        self.assertEqual(report["fully_orphan_day_ranges"], [])

    def test_album_sidecar_protects_item_without_album_media_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = directory / "takeout-20260101T000000Z-1-001.zip"
            output = directory / "orphans.json"
            root = "Takeout/Google Photos"

            with zipfile.ZipFile(archive, "w") as takeout:
                takeout.writestr(f"{root}/Photos from 2024/FILED.JPG", b"filed")
                takeout.writestr(
                    f"{root}/Photos from 2024/FILED.JPG.supplemental-metadata.json",
                    sidecar("FILED.JPG", 1704067200, "FILED"),
                )
                takeout.writestr(f"{root}/Photos from 2024/ORPHAN.JPG", b"orphan")
                takeout.writestr(
                    f"{root}/Photos from 2024/ORPHAN.JPG.supplemental-metadata.json",
                    sidecar("ORPHAN.JPG", 1704153600, "ORPHAN"),
                )
                # Takeout often places only a sidecar in an album folder. That
                # sidecar must still prove membership and protect FILED.JPG.
                takeout.writestr(
                    f"{root}/My Album/FILED.JPG.supplemental-metadata.json",
                    sidecar("FILED.JPG", 1704067200, "ALBUMCOPY"),
                )

            result = subprocess.run(
                [sys.executable, str(FINDER), str(archive), "-o", str(output)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(
                [item["title"] for item in report["orphans"]], ["ORPHAN.JPG"]
            )
            self.assertNotIn(
                "FILED.JPG", [item["title"] for item in report["orphans"]]
            )
            self.assertTrue(report["counts"]["reconciles"])


if __name__ == "__main__":
    unittest.main()
