import unittest

from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, deduplicate_candidates


class ScoringTestCase(unittest.TestCase):
    def test_score_candidates_rewards_clean_boundaries_and_complete_thought(self):
        candidates = [
            {
                "start": 0.0,
                "end": 60.0,
                "duration": 60.0,
                "text": "Por que esse erro destrói seu resultado? Vou te mostrar o passo a passo. No fim, esse é o ponto.",
                "opening_text": "Por que esse erro destrói seu resultado?",
                "middle_text": "Vou te mostrar o passo a passo com um exemplo concreto.",
                "closing_text": "No fim, esse é o ponto.",
                "segments_count": 3,
                "pause_before": 0.6,
                "pause_after": 0.8,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 61.0,
                "end": 121.0,
                "duration": 60.0,
                "text": "E aí tipo assim cara né, tipo assim, cara, aí você vai, né, tipo assim.",
                "opening_text": "E aí tipo assim cara né",
                "middle_text": "tipo assim, cara, aí você vai",
                "closing_text": "né, tipo assim",
                "segments_count": 3,
                "pause_before": 0.0,
                "pause_after": 0.0,
                "starts_clean": False,
                "ends_clean": False,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertGreater(ranked[0]["boundary_score"], ranked[1]["boundary_score"])
        self.assertLess(ranked[1]["repetition_penalty"], 0)
        self.assertIn("começa em fronteira limpa", ranked[0]["reason"])

    def test_score_candidates_applies_diversity_penalty_to_overlapping_similar_cuts(self):
        candidates = [
            {
                "start": 0.0,
                "end": 70.0,
                "duration": 70.0,
                "text": "O segredo do crescimento está nesse erro que ninguém percebe e eu vou explicar agora.",
                "opening_text": "O segredo do crescimento está nesse erro",
                "middle_text": "ninguém percebe e eu vou explicar",
                "closing_text": "agora.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.4,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 2.0,
                "end": 72.0,
                "duration": 70.0,
                "text": "O segredo do crescimento está nesse erro que ninguém percebe e eu vou explicar agora.",
                "opening_text": "O segredo do crescimento está nesse erro",
                "middle_text": "ninguém percebe e eu vou explicar",
                "closing_text": "agora.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.4,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 90.0,
                "end": 150.0,
                "duration": 60.0,
                "text": "Como corrigir isso na prática com três passos simples e um exemplo real no final.",
                "opening_text": "Como corrigir isso na prática",
                "middle_text": "com três passos simples",
                "closing_text": "e um exemplo real no final.",
                "segments_count": 3,
                "pause_before": 0.7,
                "pause_after": 0.6,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        similar_scores = [item for item in ranked if "segredo do crescimento" in item["text"].lower()]
        self.assertTrue(any(item["diversity_penalty"] > 0 for item in similar_scores))
        self.assertEqual(ranked[0]["start"], 0.0)
        self.assertEqual(ranked[1]["start"], 90.0)

    def test_build_candidate_windows_marks_boundaries_and_deduplicates_heavy_overlap(self):
        segments = [
            {"start": 0.0, "end": 10.0, "text": "Introdução sem ponto"},
            {"start": 10.1, "end": 25.0, "text": "Por que isso importa de verdade?"},
            {"start": 25.5, "end": 40.0, "text": "Vou mostrar o problema com clareza."},
            {"start": 40.7, "end": 58.0, "text": "No fim, esse é o ponto."},
            {"start": 58.1, "end": 70.0, "text": "Encerramento final."},
        ]

        candidates = build_candidate_windows(segments, mode="short")
        self.assertTrue(any(candidate["starts_clean"] for candidate in candidates))
        self.assertTrue(any(candidate["ends_clean"] for candidate in candidates))

        deduped = deduplicate_candidates(
            [
                {"start": 0.0, "end": 70.0, "duration": 70.0, "text": "a"},
                {"start": 1.0, "end": 69.0, "duration": 68.0, "text": "b"},
                {"start": 80.0, "end": 140.0, "duration": 60.0, "text": "c"},
            ]
        )
        self.assertEqual(len(deduped), 2)


if __name__ == "__main__":
    unittest.main()
