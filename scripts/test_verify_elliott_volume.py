import unittest

from verify_elliott_volume import (
    HEALTHY_CORRECTION,
    HIGH_PULLBACK_VOLUME,
    INSUFFICIENT_DATA,
    WAVE_3_LOWEST_WARNING,
    WAVE_5_DIVERGENCE,
    WEAK_WAVE_3,
    add_volume_confirmation,
)


def sequence(volumes, peaks=(10, 12, 20, 18, 22)):
    return [
        {
            "sequence_id": "test-sequence",
            "wave_label": number,
            "end_price": peaks[number - 1],
            "total_volume": volumes[number - 1],
        }
        for number in range(1, 6)
    ]


class VolumeVerificationTests(unittest.TestCase):
    def status(self, rows):
        updated, _ = add_volume_confirmation(rows)
        self.assertTrue(all("volume_notes" in row for row in updated))
        return updated[0]["volume_confirmation_status"]

    def test_weak_wave_3_warning(self):
        rows = sequence([100, 80, 90, 70, 80], peaks=(10, 8, 20, 16, 19))
        self.assertEqual(self.status(rows), WEAK_WAVE_3)

    def test_wave_3_lowest_is_warning(self):
        self.assertEqual(
            self.status(sequence([100, 80, 60, 50, 70])),
            WAVE_3_LOWEST_WARNING,
        )

    def test_healthy_corrections_pass(self):
        rows = sequence([100, 60, 140, 70, 150], peaks=(10, 8, 20, 16, 19))
        self.assertEqual(self.status(rows), HEALTHY_CORRECTION)

    def test_high_pullback_volume_warns(self):
        rows = sequence([100, 120, 150, 160, 170], peaks=(10, 8, 20, 16, 19))
        self.assertEqual(self.status(rows), HIGH_PULLBACK_VOLUME)

    def test_wave_5_divergence_is_confirmed(self):
        rows = sequence([100, 60, 200, 80, 150])
        self.assertEqual(self.status(rows), WAVE_5_DIVERGENCE)

    def test_missing_wave_is_insufficient(self):
        rows = sequence([100, 60, 200, 80, 150])[:1]
        self.assertEqual(self.status(rows), INSUFFICIENT_DATA)

    def test_partial_sequence_preserves_observed_warning(self):
        rows = sequence([100, 120, 200, 80, 150])[:3]
        self.assertEqual(self.status(rows), HIGH_PULLBACK_VOLUME)


if __name__ == "__main__":
    unittest.main()
