from herald.tts.chunker import chunk_podcast_script, split_text_into_sentences


def test_split_text_into_sentences_protects_abbreviations():
    text = "Dr. Smith met Mr. Jones vs. the committee. It was a success! How are you?"
    sentences = split_text_into_sentences(text)
    assert len(sentences) == 3
    assert sentences[0] == "Dr. Smith met Mr. Jones vs. the committee."
    assert sentences[1] == "It was a success!"
    assert sentences[2] == "How are you?"


def test_chunk_podcast_script_respects_max_chars():
    segments = [
        {
            "sequence": 1,
            "speaker": "host",
            "text": "This is paragraph one sentence one. This is paragraph one sentence two.",
        },
        {
            "sequence": 2,
            "speaker": "host",
            "text": "This is paragraph two sentence one. This is paragraph two sentence two.",
        },
    ]

    chunks = chunk_podcast_script(segments, max_chars=80)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.text) <= 80
