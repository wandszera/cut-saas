import unittest
from unittest.mock import patch

from app.services.candidates import rerank_candidates_if_enabled
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


    def test_score_candidates_rewards_clear_structure_and_specific_promise(self):
        candidates = [
            {
                "start": 0.0,
                "end": 75.0,
                "duration": 75.0,
                "text": "Primeiro, eu vou te mostrar 3 passos para corrigir esse erro. Segundo, onde quase todo mundo falha. Em resumo, esse é o ponto.",
                "opening_text": "Primeiro, eu vou te mostrar 3 passos para corrigir esse erro.",
                "middle_text": "Segundo, onde quase todo mundo falha.",
                "closing_text": "Em resumo, esse é o ponto.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 80.0,
                "end": 155.0,
                "duration": 75.0,
                "text": "Tem um jeito melhor de fazer isso, e eu vou comentando aqui de forma mais solta ao longo da conversa.",
                "opening_text": "Tem um jeito melhor de fazer isso",
                "middle_text": "eu vou comentando aqui de forma mais solta",
                "closing_text": "ao longo da conversa.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertEqual(ranked[0]["start"], 0.0)
        self.assertGreater(ranked[0]["structure_bonus"], ranked[1]["structure_bonus"])
        self.assertIn("estrutura clara de explicação", ranked[0]["reason"])

    def test_score_candidates_penalizes_context_dependency_and_promotional_cta(self):
        candidates = [
            {
                "start": 0.0,
                "end": 70.0,
                "duration": 70.0,
                "text": "Isso aqui que eu mostrei nessa tela explica tudo, como eu falei antes. Se inscreve e compartilha.",
                "opening_text": "Isso aqui que eu mostrei nessa tela explica tudo",
                "middle_text": "como eu falei antes",
                "closing_text": "Se inscreve e compartilha.",
                "segments_count": 3,
                "pause_before": 0.0,
                "pause_after": 0.0,
                "starts_clean": False,
                "ends_clean": True,
            },
            {
                "start": 75.0,
                "end": 145.0,
                "duration": 70.0,
                "text": "Por que esse erro trava seu resultado e como corrigir isso na prática com um exemplo simples no final.",
                "opening_text": "Por que esse erro trava seu resultado",
                "middle_text": "e como corrigir isso na prática",
                "closing_text": "com um exemplo simples no final.",
                "segments_count": 3,
                "pause_before": 0.4,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertEqual(ranked[0]["start"], 75.0)
        penalized = next(item for item in ranked if item["start"] == 0.0)
        self.assertLess(penalized["context_penalty"], 0)
        self.assertLess(penalized["cta_penalty"], 0)
        self.assertIn("trecho dependente de contexto externo", penalized["reason"])

    def test_score_candidates_prefers_tighter_short_over_long_short(self):
        candidates = [
            {
                "start": 0.0,
                "end": 88.0,
                "duration": 88.0,
                "text": "Por que esse erro destrói sua retenção e como corrigir isso em 3 passos sem perder clareza no final? Primeiro eu vou te mostrar onde quase todo mundo abre frio, depois como corrigir sem enrolar, e no fim qual detalhe muda a resposta do público.",
                "opening_text": "Por que esse erro destrói sua retenção",
                "middle_text": "Primeiro eu vou te mostrar onde quase todo mundo abre frio e depois como corrigir sem enrolar",
                "closing_text": "e no fim qual detalhe muda a resposta do público.",
                "segments_count": 4,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 100.0,
                "end": 235.0,
                "duration": 135.0,
                "text": "Por que esse erro destrói sua retenção e como corrigir isso em 3 passos sem perder clareza no final, com mais voltas e exemplos extras que deixam o short mais arrastado.",
                "opening_text": "Por que esse erro destrói sua retenção",
                "middle_text": "e como corrigir isso em 3 passos sem perder clareza",
                "closing_text": "com mais voltas e exemplos extras que deixam o short mais arrastado.",
                "segments_count": 4,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertEqual(ranked[0]["start"], 0.0)
        long_short = next(item for item in ranked if item["start"] == 100.0)
        self.assertLess(long_short["duration_fit_score"], ranked[0]["duration_fit_score"])
        self.assertIn("short competitivo", long_short["reason"])

    def test_score_candidates_rewards_strong_opening_over_informative_setup(self):
        candidates = [
            {
                "start": 0.0,
                "end": 72.0,
                "duration": 72.0,
                "text": "Hoje eu vou falar sobre liberdade financeira e trazer algumas reflexões iniciais antes de entrar nos detalhes.",
                "opening_text": "Hoje eu vou falar sobre liberdade financeira",
                "middle_text": "e trazer algumas reflexões iniciais",
                "closing_text": "antes de entrar nos detalhes.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 80.0,
                "end": 152.0,
                "duration": 72.0,
                "text": "Por que quase todo mundo trava na liberdade financeira? O erro está na ordem das decisões, e no fim esse é o ponto.",
                "opening_text": "Por que quase todo mundo trava na liberdade financeira?",
                "middle_text": "O erro está na ordem das decisões",
                "closing_text": "e no fim esse é o ponto.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertEqual(ranked[0]["start"], 80.0)
        informative = next(item for item in ranked if item["start"] == 0.0)
        strong = ranked[0]
        self.assertLess(informative["opening_strength_score"], strong["opening_strength_score"])
        self.assertIn("abertura mais informativa do que forte", informative["reason"])

    def test_score_candidates_penalizes_context_insufficient_opening(self):
        candidates = [
            {
                "start": 0.0,
                "end": 66.0,
                "duration": 66.0,
                "text": "Esse ponto aqui mostra exatamente o que eu te falei antes, e é por isso que isso funciona.",
                "opening_text": "Esse ponto aqui mostra exatamente",
                "middle_text": "o que eu te falei antes",
                "closing_text": "e é por isso que isso funciona.",
                "segments_count": 3,
                "pause_before": 0.0,
                "pause_after": 0.4,
                "starts_clean": False,
                "ends_clean": True,
            },
            {
                "start": 70.0,
                "end": 136.0,
                "duration": 66.0,
                "text": "O erro aqui é começar pelo tático antes da estratégia, e por isso quase todo mundo perde tempo.",
                "opening_text": "O erro aqui é começar pelo tático antes da estratégia",
                "middle_text": "e por isso quase todo mundo perde tempo",
                "closing_text": "perde tempo.",
                "segments_count": 3,
                "pause_before": 0.4,
                "pause_after": 0.4,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertEqual(ranked[0]["start"], 70.0)
        dependent = next(item for item in ranked if item["start"] == 0.0)
        self.assertLess(dependent["context_penalty"], -1.0)
        self.assertIn("começa sem referente claro", dependent["reason"])

    def test_score_candidates_penalizes_same_opening_redundancy_more_aggressively(self):
        candidates = [
            {
                "start": 0.0,
                "end": 68.0,
                "duration": 68.0,
                "text": "O erro que destrói sua retenção aparece logo no começo e eu vou te mostrar como resolver isso com clareza.",
                "opening_text": "O erro que destrói sua retenção aparece logo no começo",
                "middle_text": "e eu vou te mostrar como resolver isso",
                "closing_text": "com clareza.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 10.0,
                "end": 78.0,
                "duration": 68.0,
                "text": "O erro que destrói sua retenção aparece logo no começo, e aqui eu aprofundo quase o mesmo argumento com poucas mudanças.",
                "opening_text": "O erro que destrói sua retenção aparece logo no começo",
                "middle_text": "e aqui eu aprofundo quase o mesmo argumento",
                "closing_text": "com poucas mudanças.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 90.0,
                "end": 158.0,
                "duration": 68.0,
                "text": "Três sinais mostram que seu conteúdo abre frio demais, e o terceiro é o que mais derruba retenção.",
                "opening_text": "Três sinais mostram que seu conteúdo abre frio demais",
                "middle_text": "e o terceiro é o que mais derruba retenção",
                "closing_text": "derruba retenção.",
                "segments_count": 3,
                "pause_before": 0.5,
                "pause_after": 0.5,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]

        ranked = score_candidates(candidates, mode="short", niche="geral")

        self.assertIn(ranked[0]["start"], (0.0, 10.0))
        self.assertEqual(ranked[1]["start"], 90.0)
        redundant = next(item for item in ranked if item["start"] == 10.0)
        self.assertGreater(redundant["diversity_penalty"], 4.0)

    def test_rerank_candidates_if_enabled_uses_llm_without_breaking_fallback(self):
        candidates = [
            {"start": 0.0, "score": 8.0, "base_score": 8.0, "reason": "base", "text": "a", "opening_text": "a", "closing_text": "a", "duration": 60.0},
            {"start": 10.0, "score": 7.0, "base_score": 7.0, "reason": "base", "text": "b", "opening_text": "b", "closing_text": "b", "duration": 60.0},
        ]

        with (
            patch("app.services.candidates.settings.llm_rerank_enabled", True),
            patch(
                "app.services.candidates.analyze_candidates_with_llm",
                return_value=[
                    {**candidates[1], "llm_score": 9.5, "score": 8.5},
                    {**candidates[0], "llm_score": 7.0, "score": 7.6},
                ],
            ),
        ):
            reranked = rerank_candidates_if_enabled(candidates, mode="short")

        self.assertEqual(reranked[0]["start"], 10.0)

    def test_rerank_candidates_if_enabled_falls_back_to_heuristic_on_error(self):
        candidates = [
            {"start": 0.0, "score": 8.0, "base_score": 8.0, "reason": "base", "text": "a", "opening_text": "a", "closing_text": "a", "duration": 60.0},
        ]

        with (
            patch("app.services.candidates.settings.llm_rerank_enabled", True),
            patch("app.services.candidates.analyze_candidates_with_llm", side_effect=RuntimeError("offline")),
        ):
            reranked = rerank_candidates_if_enabled(candidates, mode="short")

        self.assertEqual(reranked, candidates)

    def test_score_candidates_uses_transcript_insights_as_context_layer(self):
        candidates = [
            {
                "start": 30.0,
                "end": 95.0,
                "duration": 65.0,
                "text": "Esse erro de precificação destrói sua margem e o resultado final do negócio.",
                "opening_text": "Esse erro de precificação destrói sua margem",
                "middle_text": "e o resultado final do negócio",
                "closing_text": "do negócio.",
                "segments_count": 3,
                "pause_before": 0.6,
                "pause_after": 0.6,
                "starts_clean": True,
                "ends_clean": True,
            },
            {
                "start": 130.0,
                "end": 190.0,
                "duration": 60.0,
                "text": "Uma conversa mais genérica sobre rotina e organização sem foco forte.",
                "opening_text": "Uma conversa mais genérica",
                "middle_text": "sobre rotina e organização",
                "closing_text": "sem foco forte.",
                "segments_count": 3,
                "pause_before": 0.6,
                "pause_after": 0.6,
                "starts_clean": True,
                "ends_clean": True,
            },
        ]
        insights = {
            "priority_keywords": ["precificação", "margem"],
            "avoid_patterns": ["rotina genérica"],
            "promising_ranges": [{"start_hint_seconds": 20, "end_hint_seconds": 100, "why": "gancho forte"}],
        }

        ranked = score_candidates(candidates, mode="short", niche="geral", transcript_insights=insights)

        self.assertEqual(ranked[0]["start"], 30.0)
        self.assertGreater(ranked[0]["transcript_context_score"], ranked[1]["transcript_context_score"])


if __name__ == "__main__":
    unittest.main()
