"""
test_image_policy.py — Tests for image policy classification and generation decisions.

Covers:
  - Lexical type heuristic classification
  - Image allowed/suppressed decisions
  - Multi-word items classified as chunk/sentence (always suppressed)
  - Concrete nouns get images
  - Function words, slang, connectors suppressed

Usage:
    python3 -m pytest test_image_policy.py -v
    python3 test_image_policy.py
"""

import unittest

from image_policy import (
    classify_lexical_type, should_generate_image, get_image_policy,
    _heuristic_classify, IMAGE_ALLOWED, LEXICAL_TYPES,
)


class TestHeuristicClassification(unittest.TestCase):
    """Tests the fast local heuristic (no GPT call)."""

    def test_function_words(self):
        for word in ['que', 'de', 'não', 'um', 'para', 'com', 'e', 'ou', 'meu']:
            result = _heuristic_classify(word)
            self.assertEqual(result, 'function_word', f"'{word}' should be function_word, got {result}")

    def test_discourse_markers(self):
        for word in ['oxe', 'vixe', 'né', 'então', 'aí', 'enfim']:
            result = _heuristic_classify(word)
            self.assertEqual(result, 'discourse_marker', f"'{word}' should be discourse_marker, got {result}")

    def test_multi_word_chunk(self):
        """2-3 word items → chunk."""
        for text in ['tô de boa', 'dar um rolê', 'por causa']:
            result = _heuristic_classify(text)
            self.assertEqual(result, 'chunk', f"'{text}' should be chunk, got {result}")

    def test_sentence(self):
        """4+ word items → sentence."""
        for text in ['eu tenho medo de cobra', 'vou dar um rolê na barra']:
            result = _heuristic_classify(text)
            self.assertEqual(result, 'sentence', f"'{text}' should be sentence, got {result}")

    def test_verb_suffix(self):
        """Common verb endings detected."""
        for word in ['fazer', 'comer', 'subir', 'lutando', 'falando', 'comido']:
            result = _heuristic_classify(word)
            self.assertEqual(result, 'action_verb', f"'{word}' should be action_verb, got {result}")

    def test_unknown_returns_none(self):
        """Unknown single words return None (needs GPT)."""
        result = _heuristic_classify('cachorro')
        self.assertIsNone(result, "Novel nouns should return None for GPT fallback")

    def test_none_input_safe(self):
        """None input should not crash."""
        result = _heuristic_classify(None)
        self.assertEqual(result, 'abstract_word')

    def test_empty_string_safe(self):
        """Empty string should not crash."""
        result = _heuristic_classify('')
        self.assertEqual(result, 'abstract_word')

    def test_whitespace_only_safe(self):
        """Whitespace-only input should not crash."""
        result = _heuristic_classify('   ')
        self.assertEqual(result, 'abstract_word')


class TestImagePolicy(unittest.TestCase):
    """Tests the should_generate_image decision layer."""

    def test_chunks_never_get_images(self):
        """Multi-word chunks should never get images."""
        for text in ['tô de boa', 'bom demais', 'por causa do barulho', 'não dá pra resistir']:
            result = should_generate_image(text)
            self.assertFalse(result, f"Chunk '{text}' should NOT get an image")

    def test_sentences_never_get_images(self):
        for text in ['ao redor da praça inteira']:
            result = should_generate_image(text)
            self.assertFalse(result, f"Sentence '{text}' should NOT get an image")

    def test_function_words_no_image(self):
        for word in ['que', 'não', 'meu', 'da']:
            result = should_generate_image(word)
            self.assertFalse(result, f"Function word '{word}' should NOT get an image")

    def test_concrete_noun_gets_image(self):
        """Known concrete nouns should get images."""
        result = should_generate_image('cachorro', lexical_type='concrete_noun')
        self.assertTrue(result)

    def test_place_gets_image(self):
        result = should_generate_image('praia', lexical_type='place')
        self.assertTrue(result)

    def test_food_gets_image(self):
        result = should_generate_image('vatapá', lexical_type='food')
        self.assertTrue(result)

    def test_abstract_no_image(self):
        result = should_generate_image('sonolento', lexical_type='abstract_word')
        self.assertFalse(result)

    def test_slang_no_image(self):
        result = should_generate_image('massa', lexical_type='slang_expression')
        self.assertFalse(result)

    def test_action_verb_no_image(self):
        """Action verbs default to no image."""
        result = should_generate_image('lutando', lexical_type='action_verb')
        self.assertFalse(result)


class TestImagePolicyMetadata(unittest.TestCase):
    """Tests get_image_policy returns correct structure."""

    def test_returns_required_keys(self):
        result = get_image_policy('tô de boa')
        self.assertIn('text', result)
        self.assertIn('lexical_type', result)
        self.assertIn('image_allowed', result)
        self.assertIn('reason', result)

    def test_chunk_policy(self):
        result = get_image_policy('dar um rolê')
        self.assertEqual(result['lexical_type'], 'chunk')
        self.assertFalse(result['image_allowed'])

    def test_concrete_policy(self):
        result = get_image_policy('mercado', lexical_type='place')
        self.assertTrue(result['image_allowed'])
        self.assertIn('concrete', result['reason'])


class TestAllTypesHavePolicy(unittest.TestCase):
    """Ensures every lexical type has an image policy mapping."""

    def test_all_types_mapped(self):
        for lt in LEXICAL_TYPES:
            self.assertIn(lt, IMAGE_ALLOWED, f"Lexical type '{lt}' missing from IMAGE_ALLOWED")

    def test_image_allowed_values_are_bool(self):
        for lt, val in IMAGE_ALLOWED.items():
            self.assertIsInstance(val, bool, f"IMAGE_ALLOWED['{lt}'] should be bool, got {type(val)}")


if __name__ == "__main__":
    unittest.main()
