"""Deterministic temporal fusion on explicit event fixtures; no ASR simulation."""

from dataclasses import FrozenInstanceError, asdict, replace
from fractions import Fraction
import json
import math

import pytest

from clip_factory.analysis import moments
from clip_factory.analysis.audio import AudioAnalysisConfig, AudioAnalysisResult, AudioEvent
from clip_factory.analysis.scenes import SceneChange, SceneDetectionResult
from clip_factory.analysis.transcript import TranscriptConfig, TranscriptResult, TranscriptSegment

SOURCE = "/local/permitted-reference.mp4"
TEXT = "Esta prueba controlada tiene suficientes palabras para localizar actividad de voz."


def visual(times=(10.0,), *, origin=0.0, base="1/1000"):
    rational = Fraction(base)
    start = int(Fraction(str(origin)) / rational)
    changes = tuple(SceneChange(int(Fraction(str(origin + time)) / rational), time,
                                origin + time, 35.0) for time in times)
    return SceneDetectionResult(SOURCE, 0, base, start, changes, 10_000,
                                10.0, 0.0, 300.0, 4, "test-contract", 0.0)


def acoustic(specs=((10.0, 10.5, "peak"),), *, origin=0.0, span=120.0):
    events = tuple(AudioEvent(kind, round((origin + first) * 1000), round((origin + last) * 1000),
                              first, last, origin + first, origin + last, 1, -1.0, -4.0,
                              12.0 if kind == "energy_rise" else -12.0 if kind == "energy_drop" else None)
                   for first, last, kind in specs)
    return AudioAnalysisResult(SOURCE, "PASS", 1, 1000, 1, "1/1000", "1/1000",
                               round(origin * 1000), 500, 1, (), events, round(span * 1000),
                               0.1, -20.0, -1.0, AudioAnalysisConfig(), 4, (), "test-contract", 0.0)


def speech(specs=((9.0, 12.0, TEXT),), *, origin=0.0, span=120.0):
    segments = tuple(TranscriptSegment(text, first, last, origin + first, origin + last)
                     for first, last, text in specs)
    return TranscriptResult(SOURCE, "PASS", None, "explicit-test-contract", None, None,
                            "es", 1, segments, (), TranscriptConfig(), 0.0, 0.0, 0.0,
                            round(origin * 16000), "1/16000", "1/1000", round(origin * 8000),
                            8000, 16, round(span * 16000), 4, sample_rate=16000)


def detect(*, scenes=None, audio=None, transcript=None, duration=120.0, origin=0.0, **settings):
    return moments.detect_moments(source=SOURCE, duration=duration, origin_seconds=origin,
                                  scenes=scenes, audio=audio, transcript=transcript,
                                  config=moments.MomentDetectionConfig(**settings))


def assert_valid(result):
    json.dumps(asdict(result), allow_nan=False)
    for candidate in result.moments:
        assert 0 <= candidate.start < candidate.end
        assert candidate.duration == candidate.end - candidate.start
        assert result.config.min_duration - 1e-8 <= candidate.duration <= result.config.max_duration + 1e-8
        if result.duration is not None:
            assert candidate.end <= result.duration + 1e-8
        assert len({item.family for item in candidate.evidence}) >= result.config.min_sources
        assert set(candidate.reasons) == {item.kind for item in candidate.evidence}
        assert len(candidate.evidence) <= 64
        for evidence in candidate.evidence:
            assert candidate.start - 1e-8 <= evidence.support_start_seconds
            assert evidence.support_start_seconds <= evidence.support_end_seconds
            assert evidence.support_end_seconds <= candidate.end + 1e-8
            assert evidence.start_seconds - 1e-8 <= evidence.support_start_seconds
            assert evidence.support_end_seconds <= evidence.end_seconds + 1e-8


def test_three_families_create_explainable_candidate_with_original_evidence():
    result = detect(scenes=visual(), audio=acoustic(), transcript=speech())
    assert result.sources_available == ("scenes", "audio", "transcription")
    assert result.sources_missing == ()
    assert result.events_considered == 3
    candidate, = result.moments
    assert (candidate.start, candidate.end) == (7.0, 15.0)
    assert set(candidate.reasons) == {"audio_peak", "scene_change", "speech_activity"}
    assert next(e for e in candidate.evidence if e.family == "audio").start_pts == 10000
    assert next(e for e in candidate.evidence if e.family == "transcription").text_excerpt == TEXT
    assert not hasattr(candidate, "score")
    assert result.engine == "deterministic.rules"
    assert_valid(result)


@pytest.mark.parametrize("omitted", ["scenes", "audio", "transcription"])
def test_any_pair_of_independent_families_can_contribute(omitted):
    inputs = {"scenes": visual(), "audio": acoustic(), "transcript": speech()}
    inputs["transcript" if omitted == "transcription" else omitted] = None
    result = detect(**inputs)
    assert len(result.moments) == 1 and result.sources_missing == (omitted,)
    assert_valid(result)


@pytest.mark.parametrize("inputs", [{}, {"scenes": visual()}, {"audio": acoustic()}, {"transcript": speech()}])
def test_no_single_family_can_create_a_candidate(inputs):
    result = detect(**inputs)
    assert result.status == "PASS" and not result.moments
    assert any("Cobertura insuficiente" in note for note in result.warnings)


def test_peak_and_energy_change_are_one_family_even_when_overlapping():
    result = detect(audio=acoustic(((10, 10.5, "peak"), (10, 10.5, "energy_rise"))))
    assert not result.moments and result.sources_available == ("audio",)


def test_same_onset_acoustic_kinds_are_all_preserved_without_duplicate_evidence():
    result = detect(scenes=visual(), audio=acoustic(((10, 10.5, "peak"), (10, 10.5, "energy_rise"))))
    candidate, = result.moments
    assert set(candidate.reasons) == {"scene_change", "audio_peak", "audio_energy_rise"}
    assert len(candidate.evidence) == len({(e.family, e.event_index) for e in candidate.evidence}) == 3
    assert_valid(result)


def test_three_required_sources_reject_pairs_but_accept_full_coincidence():
    assert not detect(scenes=visual(), audio=acoustic(), min_sources=3).moments
    assert len(detect(scenes=visual(), audio=acoustic(), transcript=speech(), min_sources=3).moments) == 1


@pytest.mark.parametrize("offset", [-1.25, 2.5, 12345.5])
def test_common_positive_and_negative_origins_preserve_relative_intervals(offset):
    plain = detect(scenes=visual(), audio=acoustic(), transcript=speech())
    result = detect(scenes=visual(origin=offset), audio=acoustic(origin=offset),
                    transcript=speech(origin=offset), origin=offset)
    assert [(m.start, m.end) for m in result.moments] == [(m.start, m.end) for m in plain.moments]
    assert next(e for e in result.moments[0].evidence if e.family == "audio").source_start_seconds == offset + 10
    assert_valid(result)


def test_different_stream_origins_align_absolute_pts_and_not_relative_floats():
    result = detect(scenes=visual(origin=2.5), audio=acoustic(((9, 9.5, "peak"),), origin=3.5),
                    transcript=speech(((8.0, 11.0, TEXT),), origin=3.5), origin=2.5)
    candidate, = result.moments
    assert (candidate.start, candidate.end) == (7, 15)
    evidence = next(e for e in candidate.evidence if e.family == "audio")
    assert evidence.start_seconds == 10 and evidence.source_start_seconds == 12.5
    assert evidence.stream_origin_seconds == 3.5
    assert_valid(result)


def test_fractional_pts_are_authoritative_without_using_fps():
    scene = replace(visual(()), time_base="1/90000", start_pts=-90000,
                    changes=(SceneChange(30003, float(Fraction(120003, 90000)), float(Fraction(30003, 90000)), None),))
    result = detect(scenes=scene, audio=acoustic(((1.334, 1.5, "peak"),), origin=-1), origin=-1)
    assert len(result.moments) == 1
    evidence = next(item for item in result.moments[0].evidence if item.family == "scenes")
    assert evidence.start_pts == 30003 and evidence.time_base == "1/90000"
    assert evidence.start_seconds == float(Fraction(120003, 90000))


def test_speech_covering_a_cut_matches_even_when_start_is_far_away():
    result = detect(scenes=visual((30,)), transcript=speech(((0, 40, TEXT),)))
    assert len(result.moments) == 1
    assert (result.moments[0].start, result.moments[0].end) == (0, 43)
    assert_valid(result)


def test_long_speech_is_localized_and_preserves_original_interval():
    result = detect(scenes=visual((60,)), transcript=speech(((0, 120, TEXT),)))
    candidate, = result.moments
    evidence = next(e for e in candidate.evidence if e.family == "transcription")
    assert (evidence.start_seconds, evidence.end_seconds) == (0, 120)
    assert candidate.start <= 60 <= candidate.end and candidate.duration < 45
    assert any("Voz prolongada" in note for note in result.warnings)
    assert_valid(result)


@pytest.mark.parametrize("spec", [(9, 9.5, TEXT), (9, 12, "wow"), (9, 12, "one two three four five"), (9, 12, "!!!")])
def test_short_or_isolated_speech_is_insufficient(spec):
    result = detect(scenes=visual(), transcript=speech((spec,)))
    assert not result.moments
    assert any("voz breves" in note for note in result.warnings)


@pytest.mark.parametrize("text", ["uno, dos; tres: cuatro. cinco seis", "árbol café niño acción sí mañana", "one two three four five six"])
def test_word_threshold_is_unicode_aware_and_independent_of_language(text):
    result = detect(scenes=visual(), transcript=speech(((9, 12, text),)))
    assert len(result.moments) == 1


@pytest.mark.parametrize("kind", ["peak", "energy_rise", "energy_drop"])
def test_sustained_acoustic_events_do_not_use_late_overlap_as_an_onset(kind):
    result = detect(scenes=visual((30,)), audio=acoustic(((0, 60, kind),)))
    assert not result.moments
    assert any("sostenidos" in note for note in result.warnings)


def test_silence_events_are_not_a_reason_for_interest():
    result = detect(scenes=visual(), audio=acoustic(((9, 12, "silence"),)))
    assert result.sources_available == ("scenes", "audio")
    assert not result.moments


def test_empty_pass_sources_are_available_but_create_no_candidates():
    result = detect(scenes=visual(()), audio=acoustic(()), transcript=speech(()))
    assert result.sources_available == ("scenes", "audio", "transcription")
    assert not result.sources_missing and not result.moments and result.events_considered == 0


def test_skipped_sources_remain_missing_and_preserve_warnings():
    audio = replace(acoustic(()), status="SKIPPED", warnings=("No audio",))
    transcript = replace(speech(()), status="SKIPPED", skip_reason="engine_unavailable", warnings=("Missing engine",))
    result = detect(scenes=visual(), audio=audio, transcript=transcript)
    assert result.sources_missing == ("audio", "transcription") and not result.moments
    assert any("Missing engine" in note for note in result.warnings)


@pytest.mark.parametrize("distance,expected", [(0, 1), (1, 1), (1.001, 0), (5, 0)])
def test_coincidence_window_is_inclusive_and_not_global_clustering(distance, expected):
    result = detect(scenes=visual(), audio=acoustic(((10 + distance, 10.5 + distance, "peak"),)))
    assert len(result.moments) == expected


def test_three_families_must_be_pairwise_close_not_a_transitive_chain():
    result = detect(scenes=visual((10,)), audio=acoustic(((9, 9.5, "peak"),)),
                    transcript=speech(((11, 14, TEXT),)), min_sources=3)
    assert not result.moments


def test_context_overlap_merges_candidates_and_keeps_all_reasons_and_indices():
    result = detect(scenes=visual((10, 13)), audio=acoustic(((10, 10.5, "peak"), (13, 13.5, "energy_rise"))))
    candidate, = result.moments
    assert (candidate.start, candidate.end) == (8, 16.5)
    assert len(candidate.evidence) == 4
    assert set(candidate.reasons) == {"scene_change", "audio_peak", "audio_energy_rise"}
    assert_valid(result)


def test_long_chain_never_merges_beyond_maximum_duration():
    times = tuple(range(5, 110, 3))
    result = detect(scenes=visual(times), audio=acoustic(tuple((t, t + .5, "peak") for t in times)),
                    max_duration=12, cooldown=0, max_candidates=100)
    assert len(result.moments) > 1
    assert all(m.duration <= 12 for m in result.moments)
    assert_valid(result)


def test_overlapping_groups_that_cannot_merge_keep_only_the_first_chronologically():
    result = detect(scenes=visual((10, 14)), audio=acoustic(((10, 10.5, "peak"), (14, 14.5, "peak"))),
                    min_duration=5, max_duration=6, context_before=2, context_after=3, cooldown=0)
    assert len(result.moments) == 1 and result.moments[0].start == 8
    assert any("candidatos solapados" in note for note in result.warnings)
    assert result.candidates_suppressed > 0


@pytest.mark.parametrize("cut,audio_spec,voice_spec,maximum", [
    (56, (56, 57, "peak"), (10, 55, TEXT), 45),
    (10, (10, 14, "peak"), (8, 12, TEXT), 5),
])
def test_optional_speech_never_erases_a_viable_visual_audio_pair(cut, audio_spec, voice_spec, maximum):
    plain = detect(scenes=visual((cut,)), audio=acoustic((audio_spec,)), max_duration=maximum)
    enriched = detect(scenes=visual((cut,)), audio=acoustic((audio_spec,)),
                      transcript=speech((voice_spec,)), max_duration=maximum)
    assert plain.moments and enriched.moments
    assert {"scene_change", "audio_peak"} <= set(enriched.moments[0].reasons)
    assert_valid(enriched)


def test_unfit_triple_falls_back_to_viable_pair_only_when_two_sources_are_allowed():
    settings = dict(min_duration=1, max_duration=1, context_before=0, context_after=0,
                    min_speech_duration=2, coincidence_window=1)
    inputs = dict(scenes=visual((10,)), audio=acoustic(((10.8, 11.8, "peak"),)),
                  transcript=speech(((9, 12, TEXT),)))
    pair = detect(**inputs, **settings)
    strict = detect(**inputs, **settings, min_sources=3)
    assert pair.moments and not strict.moments
    assert_valid(pair)


def test_large_original_pts_retain_small_rational_relative_offsets():
    offset = 1_000_000_000
    base = Fraction(1, 90000)
    start_pts = offset * 90000 + 1
    cut_pts = start_pts + 901
    scene = replace(visual(()), time_base="1/90000", start_pts=start_pts,
                    changes=(SceneChange(cut_pts, float(Fraction(901, 90000)), float(cut_pts * base), 40),))
    # Scene origin has an extra fractional tick; audio is on its own sample grid.
    audio = acoustic(((.010, .510, "peak"),), origin=offset)
    voice = speech(((0, 3, TEXT),), origin=offset)
    result = detect(scenes=scene, audio=audio, transcript=voice,
                    origin=float(start_pts * base), duration=10)
    evidence = {e.family: e for e in result.moments[0].evidence}
    assert evidence["scenes"].start_seconds == float(Fraction(901, 90000))
    assert evidence["audio"].start_seconds == float(Fraction(10, 1000) - Fraction(1, 90000))
    assert evidence["transcription"].start_seconds == -float(Fraction(1, 90000))
    assert_valid(result)


def test_cooldown_is_chronological_and_never_a_viral_score():
    result = detect(scenes=visual((5, 11, 25)), audio=acoustic(((5, 5.5, "peak"), (11, 11.5, "peak"), (25, 25.5, "peak"))),
                    min_duration=2, max_duration=3, context_before=0, context_after=0, merge_gap=0, cooldown=10)
    assert len(result.moments) == 2
    assert any("cooldown" in note for note in result.warnings)
    assert_valid(result)


def test_candidate_limit_preserves_first_chronological_candidates_with_warning():
    times = (10, 30, 50, 70, 90)
    result = detect(scenes=visual(times), audio=acoustic(tuple((t, t + .5, "peak") for t in times)), max_candidates=2)
    assert [m.start for m in result.moments] == [8, 28]
    assert any("max_candidates" in note for note in result.warnings)


def test_thousands_of_repetitive_events_have_bounded_output_and_evidence():
    times = tuple(i / 100 for i in range(1, 2001))
    result = detect(scenes=visual(times), audio=acoustic(tuple((t, t + .005, "peak") for t in times)),
                    max_candidates=3, cooldown=10)
    assert result.events_considered == 4000 and 0 < len(result.moments) <= 3
    assert result.candidates_suppressed > 1000
    assert any("64 evidencias" in note for note in result.warnings)
    assert_valid(result)


@pytest.mark.parametrize("first,last,duration", [(0.1, .6, 20), (19, 19.5, 20), (1, 1.5, 3)])
def test_context_and_minimum_duration_respect_video_bounds(first, last, duration):
    result = detect(scenes=visual((first,)), audio=acoustic(((first, last, "peak"),)), duration=duration)
    if duration < 5:
        assert not result.moments and any("min_duration" in note for note in result.warnings)
    else:
        assert len(result.moments) == 1
    assert_valid(result)


def test_unknown_duration_is_explicit_and_does_not_invent_a_video_end():
    result = detect(scenes=visual((119,)), audio=acoustic(((119, 119.5, "peak"),)), duration=None)
    assert result.duration is None and result.moments[0].end == 122.5
    assert any("no está verificado" in note for note in result.warnings)


def test_audio_outside_video_does_not_contribute_a_proximity_before_zero():
    result = detect(scenes=visual((.25,)), audio=acoustic(((0, .5, "peak"),), origin=-.5))
    assert not result.moments and any("fuera de la línea temporal" in note for note in result.warnings)


def test_audio_and_transcription_different_tracks_warn_but_keep_absolute_alignment():
    result = detect(audio=replace(acoustic(), stream_index=2), transcript=speech())
    assert len(result.moments) == 1 and any("pistas distintas" in note for note in result.warnings)


def test_evidence_text_is_bounded_and_normalized_without_mutating_input():
    text = "  prueba\n" * 1000
    original = speech(((9, 12, text),))
    result = detect(scenes=visual(), transcript=original)
    excerpt = next(e.text_excerpt for e in result.moments[0].evidence if e.family == "transcription")
    assert len(excerpt) <= 240 and "\n" not in excerpt
    assert original.segments[0].text == text


def test_immutable_models_and_determinism_except_observed_elapsed_time():
    first = detect(scenes=visual(), audio=acoustic())
    second = detect(scenes=visual(), audio=acoustic())
    assert replace(first, elapsed_seconds=0) == replace(second, elapsed_seconds=0)
    for item, field, value in ((first, "status", "FAIL"), (first.config, "min_sources", 1),
                               (first.moments[0], "start", 123), (first.moments[0].evidence[0], "kind", "invented")):
        with pytest.raises(FrozenInstanceError):
            setattr(item, field, value)


@pytest.mark.parametrize("name,value", [
    ("min_duration", 0), ("min_duration", -1), ("max_duration", 4), ("max_duration", float("nan")),
    ("context_before", -1), ("context_after", float("inf")), ("coincidence_window", -1), ("merge_gap", -1),
    ("cooldown", -1), ("timeout", 0), ("timeout", "10"), ("timeout", True), ("min_sources", 1),
    ("min_sources", 4), ("min_sources", 2.0), ("min_sources", True), ("min_speech_duration", 0),
    ("min_speech_words", 1), ("min_speech_words", True), ("min_speech_words", 10001),
    ("max_audio_event_duration", 0), ("max_events", 0), ("max_events", 1000001), ("max_events", 3.2),
    ("max_candidates", 0), ("max_candidates", 10001), ("max_candidates", True),
])
def test_invalid_configuration_is_rejected(name, value):
    with pytest.raises(ValueError):
        moments.MomentDetectionConfig(**{name: value})


@pytest.mark.parametrize("kwargs", [{"source": ""}, {"source": None}, {"source": "x\0y"}, {"origin_seconds": float("nan")},
                                    {"origin_seconds": True}, {"duration": 0}, {"duration": -1}, {"duration": "12"}, {"duration": float("inf")}, {"config": {"min_sources": 2}}])
def test_invalid_detector_arguments_fail(kwargs):
    options = dict(source=SOURCE, duration=120)
    options.update(kwargs)
    with pytest.raises(ValueError):
        moments.detect_moments(**options)


@pytest.mark.parametrize("family,result", [("scenes", visual()), ("audio", acoustic()), ("transcript", speech())])
def test_mismatched_source_is_rejected_even_without_enough_coverage(family, result):
    with pytest.raises(moments.MomentDetectionError, match="otro archivo"):
        detect(**{family: replace(result, source="/different.mp4")})


@pytest.mark.parametrize("family,result", [("audio", acoustic()), ("transcript", speech())])
def test_failed_status_never_becomes_successful_empty_detection(family, result):
    with pytest.raises(moments.MomentDetectionError, match="no finalizó"):
        detect(**{family: replace(result, status="FAIL")})


@pytest.mark.parametrize("family,result", [("audio", acoustic()), ("transcript", speech())])
def test_skipped_source_with_events_is_malformed(family, result):
    with pytest.raises(moments.MomentDetectionError, match="omitido contiene"):
        detect(**{family: replace(result, status="SKIPPED")})


@pytest.mark.parametrize("base", [None, "", "0/1", "-1/1000", "1/0", "bogus", 0.1])
def test_invalid_rational_timestamps_are_rejected(base):
    with pytest.raises(moments.MomentDetectionError, match="base racional"):
        detect(scenes=replace(visual(), time_base=base))


def test_video_origin_mismatch_is_not_silently_rebased():
    with pytest.raises(moments.MomentDetectionError, match="origin_seconds/video"):
        detect(scenes=visual(origin=2.5))


@pytest.mark.parametrize("field,value", [("timestamp_seconds", 999), ("source_timestamp_seconds", 999), ("score", float("inf")), ("score", 101), ("pts", 0), ("pts", True)])
def test_bad_scene_event_cannot_generate_a_candidate(field, value):
    scene = visual()
    scene = replace(scene, changes=(replace(scene.changes[0], **{field: value}),))
    with pytest.raises(moments.MomentDetectionError):
        detect(scenes=scene, audio=acoustic())


def test_scene_event_order_is_audited():
    with pytest.raises(moments.MomentDetectionError, match="fuera de orden"):
        detect(scenes=visual((10, 9)))


@pytest.mark.parametrize("field,value", [("start_seconds", 20), ("source_start_seconds", 20), ("end_seconds", 20), ("source_end_seconds", 20),
                                        ("end_pts", 9999), ("peak_dbfs", None), ("peak_dbfs", float("nan")), ("window_count", 0), ("kind", "emotion")])
def test_bad_audio_event_cannot_generate_a_candidate(field, value):
    result = acoustic()
    with pytest.raises(moments.MomentDetectionError):
        detect(scenes=visual(), audio=replace(result, events=(replace(result.events[0], **{field: value}),)))


@pytest.mark.parametrize("kind,delta", [("energy_rise", None), ("energy_rise", -1), ("energy_drop", None), ("energy_drop", 1)])
def test_audio_change_direction_is_audited(kind, delta):
    result = acoustic(((10, 10.5, kind),))
    with pytest.raises(moments.MomentDetectionError, match="magnitud/dirección"):
        detect(audio=replace(result, events=(replace(result.events[0], change_db=delta),)))


def test_audio_order_and_sample_coverage_are_audited():
    for result in (acoustic(((10, 10.5, "peak"), (9, 9.5, "peak"))), acoustic(((10, 121, "peak"),))):
        with pytest.raises(moments.MomentDetectionError, match="cobertura"):
            detect(audio=result)


@pytest.mark.parametrize("field,value", [("start_seconds", -1), ("end_seconds", 9), ("end_seconds", 121),
                                        ("source_start_seconds", 20), ("source_end_seconds", 20), ("text", ""), ("text", None), ("text", "x\0y")])
def test_bad_transcript_segment_is_rejected_even_if_it_would_not_qualify(field, value):
    transcript = speech()
    with pytest.raises(moments.MomentDetectionError):
        detect(transcript=replace(transcript, segments=(replace(transcript.segments[0], **{field: value}),)))


def test_overlapping_or_unsorted_transcript_is_rejected():
    with pytest.raises(moments.MomentDetectionError, match="fuera de orden"):
        detect(transcript=speech(((9, 12, TEXT), (11, 14, TEXT))))


def test_original_audio_and_resampled_transcript_origins_are_audited():
    with pytest.raises(moments.MomentDetectionError, match="resampleado"):
        detect(transcript=replace(speech(), source_start_pts=8000))


def test_event_limit_includes_filtered_events_and_returns_no_partial_success():
    with pytest.raises(moments.MomentDetectionError, match="max_events"):
        detect(scenes=visual(), audio=acoustic(((1, 2, "silence"), (10, 10.5, "peak"))), max_events=2)


def test_timeout_is_global_and_checked_during_validation(monkeypatch):
    ticks = iter(i / 100 for i in range(100))
    monkeypatch.setattr(moments, "monotonic", lambda: next(ticks))
    with pytest.raises(moments.MomentDetectionError, match="timeout"):
        detect(scenes=visual(), audio=acoustic(), timeout=.015)


def test_timeout_is_checked_while_counting_a_long_transcript(monkeypatch):
    ticks = iter(i / 100 for i in range(100))
    monkeypatch.setattr(moments, "monotonic", lambda: next(ticks))
    with pytest.raises(moments.MomentDetectionError, match="timeout"):
        detect(transcript=speech(((1, 5, "word " * 5000),)), timeout=.035)


def test_pure_detector_never_decodes_or_transcribes(monkeypatch):
    import clip_factory.analysis.audio as audio_module
    import clip_factory.analysis.scenes as scene_module
    import clip_factory.analysis.transcript as transcript_module
    def forbidden(*args, **kwargs):
        pytest.fail("Pure detector ran a multimedia engine")
    monkeypatch.setattr(audio_module, "analyze_audio", forbidden)
    monkeypatch.setattr(scene_module, "detect_scenes", forbidden)
    monkeypatch.setattr(transcript_module, "transcribe_media", forbidden)
    assert len(detect(scenes=visual(), audio=acoustic()).moments) == 1
