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
