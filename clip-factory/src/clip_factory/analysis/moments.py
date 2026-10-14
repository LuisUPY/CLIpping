"""Deterministic candidates from existing analysis data; no IO or inference.

Two independent signal families must coincide. Original PTS determine the
shared video timeline. Measurements and speech activity do not prove narrative
quality, emotions or viral potential. No multimedia engine is run here.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
import math
import re
from time import monotonic

from clip_factory.analysis.audio import AudioAnalysisResult
from clip_factory.analysis.scenes import SceneDetectionResult
from clip_factory.analysis.transcript import TranscriptResult
from clip_factory.media.ffmpeg import MediaError

_FAMILIES = ("scenes", "audio", "transcription")
_AUDIO_KINDS = {"peak": "audio_peak", "energy_rise": "audio_energy_rise",
                "energy_drop": "audio_energy_drop", "silence": "silence"}
_EVIDENCE_LIMIT = 64


class MomentDetectionError(MediaError):
    """Malformed input, incompatible timelines or execution limits."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} debe ser un número finito.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} debe ser un número finito.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} debe ser un número finito.")
    return number


@dataclass(frozen=True)
class MomentDetectionConfig:
    min_duration: float = 5.0
    max_duration: float = 45.0
    context_before: float = 2.0
    context_after: float = 3.0
    coincidence_window: float = 1.0
    merge_gap: float = 1.0
    cooldown: float = 10.0
    min_sources: int = 2
    min_speech_duration: float = 2.0
    min_speech_words: int = 6
    max_audio_event_duration: float = 4.0
    max_candidates: int = 20
    max_events: int = 100_000
    timeout: float = 10.0

    def __post_init__(self) -> None:
        positive = ("min_duration", "max_duration", "min_speech_duration",
                    "max_audio_event_duration", "timeout")
        for name in positive + ("context_before", "context_after", "coincidence_window", "merge_gap", "cooldown"):
            value = _finite(getattr(self, name), name)
            if (name in positive and value <= 0) or (name not in positive and value < 0):
                raise ValueError(f"{name} fuera del rango permitido.")
            object.__setattr__(self, name, value)
        if self.max_duration < self.min_duration:
            raise ValueError("max_duration debe ser >= min_duration.")
        if type(self.min_sources) is not int or self.min_sources not in (2, 3):
            raise ValueError("min_sources debe ser 2 o 3 familias distintas.")
        for name, minimum, maximum in (("min_speech_words", 2, 10_000),
                                       ("max_candidates", 1, 10_000), ("max_events", 1, 1_000_000)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} debe ser un entero entre {minimum} y {maximum}.")


@dataclass(frozen=True, slots=True)
class MomentEvidence:
    """Original event plus the portion used, inside its candidate.

    start/end_seconds normalize the original interval to video origin.
    support_start/end_seconds localize long speech without word alignment.
    text_excerpt is context for the original segment, not aligned words.
    """
    family: str
    kind: str
    event_index: int
    stream_index: int
    time_base: str
    stream_start_pts: int
    start_pts: int | None
    end_pts: int | None
    stream_origin_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    start_seconds: float
    end_seconds: float
    support_start_seconds: float
    support_end_seconds: float
    score: float | None = None
    peak_dbfs: float | None = None
    max_rms_dbfs: float | None = None
    change_db: float | None = None
    word_count: int | None = None
    text_excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class MomentCandidate:
    start: float
    end: float
    duration: float
    reasons: tuple[str, ...]
    evidence: tuple[MomentEvidence, ...]


@dataclass(frozen=True)
class MomentDetectionResult:
    source: str
    status: str
    moments: tuple[MomentCandidate, ...]
    warnings: tuple[str, ...]
    sources_available: tuple[str, ...]
    sources_missing: tuple[str, ...]
    config: MomentDetectionConfig
    elapsed_seconds: float
    events_considered: int
    candidates_suppressed: int
    origin_seconds: float
    duration: float | None
    engine: str = "deterministic.rules"
    engine_version: str = "1"


@dataclass(frozen=True, slots=True)
class _Event:
    family: str
    kind: str
    index: int
    stream: int
    base: str
    origin_pts: int
    pts: int | None
    end_pts: int | None
    origin: float
    absolute: float
    absolute_end: float
    start: float
    end: float
    score: float | None = None
    peak: float | None = None
    rms: float | None = None
    change: float | None = None
    words: int | None = None
    text: str | None = None

    @property
    def match_end(self) -> float:
        # An acoustic event contributes its onset, not a loud plateau.
        return self.end if self.family == "transcription" else self.start

    @property
    def key(self) -> tuple[str, int]:
        return self.family, self.index


@dataclass(frozen=True, slots=True)
class _Support:
    event: _Event
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class _Seed:
    start: float
    end: float
    supports: tuple[_Support, ...]


def _number(value: object, name: str) -> float:
    try:
        return _finite(value, name)
    except ValueError as exc:
        raise MomentDetectionError(str(exc)) from exc


def _integer(value: object, name: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise MomentDetectionError(f"{name} no es un entero válido.")
    return value


def _base(value: object, name: str) -> Fraction:
    try:
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError
        result = Fraction(value)
        if result <= 0 or not math.isfinite(float(result)):
            raise ValueError
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise MomentDetectionError(f"{name} no es una base racional positiva.") from exc
    return result


def _seconds(pts: int, base: Fraction) -> float:
    try:
        value = float(pts * base)
    except OverflowError as exc:
        raise MomentDetectionError("PTS fuera del rango temporal representable.") from exc
    if not math.isfinite(value):
        raise MomentDetectionError("PTS no finitos.")
    return value


def _equal(actual: object, expected: float, name: str) -> None:
    value = _number(actual, name)
    _number(expected, name + "/PTS")
    if abs(value - expected) > max(1e-7, 4 * math.ulp(expected), 4 * math.ulp(value)):
        raise MomentDetectionError(f"{name} no coincide con PTS/origen; sincronización rechazada.")


def _level(value: object, name: str) -> float | None:
    if value is None:
        return None
    number = _number(value, name)
    if not -1000 <= number <= 1000:
        raise MomentDetectionError(f"{name} fuera de rango.")
    return number


def _distance(first: _Event, second: _Event) -> float:
    return max(0.0, first.start - second.match_end, second.start - first.match_end)


class _Index:
    """At most two neighbors; speech intervals have been checked for overlap."""
    def __init__(self, events: list[_Event]) -> None:
        self.events = events
        self.starts = [event.start for event in events]

    def near(self, anchor: _Event, window: float) -> tuple[_Event, ...]:
        position = bisect_right(self.starts, anchor.start)
        return tuple(event for event in self.events[max(0, position - 1):position + 1]
                     if _distance(anchor, event) <= window + 1e-9)


def _evidence(support: _Support) -> MomentEvidence:
    e = support.event
    return MomentEvidence(e.family, e.kind, e.index, e.stream, e.base, e.origin_pts,
                          e.pts, e.end_pts, e.origin, e.absolute, e.absolute_end,
                          e.start, e.end, support.start, support.end, e.score,
                          e.peak, e.rms, e.change, e.words, e.text)


def detect_moments(
    *, source: str, duration: float | None, origin_seconds: float = 0.0,
    scenes: SceneDetectionResult | None = None, audio: AudioAnalysisResult | None = None,
    transcript: TranscriptResult | None = None, config: MomentDetectionConfig | None = None,
) -> MomentDetectionResult:
    """Reuse completed analysis, without running engines or reading files.

    origin_seconds is the first decoded VIDEO PTS in original source seconds,
    not the container start or first audio sample. duration=None leaves the
    upper video bound explicitly unverified. All events, including filtered
    ones, are validated before publishing candidates. Empty PASS and missing
    or SKIPPED sources remain distinct.
    """
    config = MomentDetectionConfig() if config is None else config
    if not isinstance(config, MomentDetectionConfig):
        raise ValueError("config debe ser MomentDetectionConfig.")
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise ValueError("source debe identificar el mismo archivo de los analizadores.")
    origin = _finite(origin_seconds, "origin_seconds")
    origin_fraction = Fraction(str(origin))
    if duration is not None:
        duration = _finite(duration, "duration")
        if duration <= 0:
            raise ValueError("duration debe ser mayor que cero o null.")
    started = monotonic()
    deadline = started + config.timeout

    def check() -> None:
        if monotonic() >= deadline:
            raise MomentDetectionError("Moment Detection superó el timeout configurado.")

    warnings: list[str] = []

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    if duration is None:
        warn("Duración del video desconocida; el límite final de los candidatos no está verificado.")
    available: list[str] = []
    events: list[_Event] = []
    count = outside = long_audio = short_speech = 0

    def validate_source(result: object, family: str, expected: type) -> bool:
        if result is None:
            return False
        if not isinstance(result, expected):
            raise MomentDetectionError(f"Resultado {family} incompatible.")
        if result.source != source:
            raise MomentDetectionError(f"{family} pertenece a otro archivo.")
        status = getattr(result, "status", "PASS")
        if status not in {"PASS", "SKIPPED"}:
            raise MomentDetectionError(f"{family} no finalizó correctamente: {status}.")
        notes = getattr(result, "warnings", ())
        if not isinstance(notes, tuple) or any(not isinstance(note, str) for note in notes):
            raise MomentDetectionError(f"Advertencias de {family} inválidas.")
        for note in notes[:16]:
            warn(f"{family}: {note[:1000]}")
        if len(notes) > 16:
            warn(f"{family}: {len(notes) - 16} advertencias adicionales resumidas.")
        if status == "SKIPPED":
            if any(getattr(result, field, ()) for field in ("events", "segments", "windows")):
                raise MomentDetectionError(f"{family} omitido contiene eventos; resultado incoherente.")
            return False
        available.append(family)
        return True

    def received(items: object, name: str) -> tuple:
        nonlocal count
        if not isinstance(items, tuple):
            raise MomentDetectionError(f"{name} debe ser una tupla inmutable.")
        count += len(items)
        if count > config.max_events:
            raise MomentDetectionError("Se alcanzó max_events; no se publicará una salida parcial.")
        check()
        return items

    if validate_source(scenes, "scenes", SceneDetectionResult):
        assert scenes is not None
        base = _base(scenes.time_base, "scenes.time_base")
        start_pts = _integer(scenes.start_pts, "scenes.start_pts")
        stream = _integer(scenes.stream_index, "scenes.stream_index", 0)
        _integer(scenes.frames_analyzed, "scenes.frames_analyzed", 1)
        stream_origin = _seconds(start_pts, base)
        _equal(origin, stream_origin, "origin_seconds/video")
        # Preserve the exact decoded origin: subtracting two large floats can
        # erase sub-frame precision even when each serialized PTS is valid.
        origin_fraction = start_pts * base
        previous = start_pts
        for index, change in enumerate(received(scenes.changes, "scenes.changes")):
            check()
            try:
                pts = _integer(change.pts, "scene.pts")
                if pts <= previous:
                    raise MomentDetectionError("Cambios visuales fuera de orden o anteriores al origen.")
                absolute = _seconds(pts, base)
                _equal(change.source_timestamp_seconds, absolute, "scene.source_timestamp_seconds")
                _equal(change.timestamp_seconds, _seconds(pts - start_pts, base), "scene.timestamp_seconds")
                score = None if change.score is None else _number(change.score, "scene.score")
                if score is not None and not 0 <= score <= 100:
                    raise MomentDetectionError("scene.score fuera de rango.")
                relative = _seconds(1, pts * base - origin_fraction)
                if duration is not None and relative > duration + 1e-7:
                    raise MomentDetectionError("Cambio visual posterior al final del video declarado.")
                events.append(_Event("scenes", "scene_change", index, stream, scenes.time_base,
                                     start_pts, pts, pts, stream_origin, absolute, absolute,
                                     max(0.0, relative), max(0.0, relative), score=score))
                previous = pts
            except AttributeError as exc:
                raise MomentDetectionError("Evento visual malformado.") from exc

    if validate_source(audio, "audio", AudioAnalysisResult):
        assert audio is not None
        base = _base(audio.time_base, "audio.time_base")
        _base(audio.source_time_base, "audio.source_time_base")
        rate = _integer(audio.sample_rate, "audio.sample_rate", 1)
        if base != Fraction(1, rate):
            raise MomentDetectionError("audio.time_base no corresponde a sample_rate.")
        start_pts = _integer(audio.start_pts, "audio.start_pts")
        stream = _integer(audio.stream_index, "audio.stream_index", 0)
        samples = _integer(audio.samples_analyzed, "audio.samples_analyzed", 1)
        tolerance = _integer(audio.timestamp_tolerance_samples, "audio.timestamp_tolerance_samples", 0)
        stream_origin = _seconds(start_pts, base)
        previous = start_pts
        for index, event in enumerate(received(audio.events, "audio.events")):
            check()
            try:
                first = _integer(event.start_pts, "audio.event.start_pts")
                last = _integer(event.end_pts, "audio.event.end_pts")
                if first < previous or first < start_pts - tolerance or last <= first or last > start_pts + samples + tolerance:
                    raise MomentDetectionError("Evento acústico fuera de orden, cobertura o duración válida.")
                if event.kind not in _AUDIO_KINDS:
                    raise MomentDetectionError("Tipo de evento acústico desconocido.")
                _integer(event.window_count, "audio.event.window_count", 1)
                absolute, absolute_end = _seconds(first, base), _seconds(last, base)
                _equal(event.source_start_seconds, absolute, "audio.source_start_seconds")
                _equal(event.source_end_seconds, absolute_end, "audio.source_end_seconds")
                _equal(event.start_seconds, _seconds(first - start_pts, base), "audio.start_seconds")
                _equal(event.end_seconds, _seconds(last - start_pts, base), "audio.end_seconds")
                peak, rms, delta = (_level(value, name) for value, name in (
                    (event.peak_dbfs, "audio.peak_dbfs"), (event.max_rms_dbfs, "audio.max_rms_dbfs"), (event.change_db, "audio.change_db")))
                if event.kind == "peak" and peak is None:
                    raise MomentDetectionError("Pico acústico sin medición.")
                if (event.kind == "energy_rise" and (delta is None or delta <= 0)) or (event.kind == "energy_drop" and (delta is None or delta >= 0)):
                    raise MomentDetectionError("Cambio acústico sin magnitud/dirección válida.")
                previous = first
                if event.kind == "silence":
                    continue
                relative = _seconds(1, first * base - origin_fraction)
                relative_end = _seconds(1, last * base - origin_fraction)
                if relative < -1e-7 or (duration is not None and relative >= duration):
                    outside += 1
                    continue
                if _seconds(last - first, base) > config.max_audio_event_duration + 1e-9:
                    long_audio += 1
                    continue
                events.append(_Event("audio", _AUDIO_KINDS[event.kind], index, stream,
                                     audio.time_base, start_pts, first, last, stream_origin,
                                     absolute, absolute_end, max(0.0, relative), relative_end,
                                     peak=peak, rms=rms, change=delta))
            except (AttributeError, TypeError) as exc:
                raise MomentDetectionError("Evento acústico malformado.") from exc

    if validate_source(transcript, "transcription", TranscriptResult):
        assert transcript is not None
        base = _base(transcript.time_base, "transcription.time_base")
        _base(transcript.source_time_base, "transcription.source_time_base")
        start_pts = _integer(transcript.start_pts, "transcription.start_pts")
        stream = _integer(transcript.stream_index, "transcription.stream_index", 0)
        rate = _integer(transcript.sample_rate, "transcription.sample_rate", 1)
        source_rate = _integer(transcript.source_sample_rate, "transcription.source_sample_rate", 1)
        source_start = _integer(transcript.source_start_pts, "transcription.source_start_pts")
        samples = _integer(transcript.samples_analyzed, "transcription.samples_analyzed", 1)
        tolerance = _integer(transcript.timestamp_tolerance_samples, "transcription.timestamp_tolerance_samples", 0)
        if base != Fraction(1, rate):
            raise MomentDetectionError("transcription.time_base no corresponde a sample_rate.")
        stream_origin = _seconds(start_pts, base)
        if abs(stream_origin - _seconds(source_start, Fraction(1, source_rate))) > max(tolerance / rate, 1e-7):
            raise MomentDetectionError("El origen resampleado no coincide con PTS del audio original.")
        if _number(transcript.timestamp_resolution_seconds, "transcription.timestamp_resolution_seconds") <= 0:
            raise MomentDetectionError("Resolución de transcripción inválida.")
        previous = 0.0
        for index, segment in enumerate(received(transcript.segments, "transcription.segments")):
            check()
            try:
                first = _number(segment.start_seconds, "transcription.start_seconds")
                last = _number(segment.end_seconds, "transcription.end_seconds")
                if first < previous - 1e-7 or first < 0 or last <= first or last > samples / rate + 1e-7:
                    raise MomentDetectionError("Segmento de voz fuera de orden, cobertura o duración válida.")
                first_fraction, last_fraction = Fraction(str(first)), Fraction(str(last))
                absolute = _seconds(1, start_pts * base + first_fraction)
                absolute_end = _seconds(1, start_pts * base + last_fraction)
                _equal(segment.source_start_seconds, absolute, "transcription.source_start_seconds")
                _equal(segment.source_end_seconds, absolute_end, "transcription.source_end_seconds")
                if not isinstance(segment.text, str) or not segment.text.strip() or "\x00" in segment.text:
                    raise MomentDetectionError("Texto de transcripción inválido.")
                words = 0
                for _ in re.finditer(r"[^\W_]+(?:['’-][^\W_]+)*", segment.text, re.UNICODE):
                    words += 1
                    if words % 256 == 0:
                        check()
                previous = last
                relative = _seconds(1, start_pts * base + first_fraction - origin_fraction)
                relative_end = _seconds(1, start_pts * base + last_fraction - origin_fraction)
                clipped_start = max(0.0, relative)
                clipped_end = min(relative_end, duration) if duration is not None else relative_end
                if clipped_end <= clipped_start:
                    outside += 1
                    continue
                if clipped_end - clipped_start < config.min_speech_duration or words < config.min_speech_words:
                    short_speech += 1
                    continue
                events.append(_Event("transcription", "speech_activity", index, stream,
                                     transcript.time_base, start_pts, None, None, stream_origin,
                                     absolute, absolute_end, relative, relative_end,
                                     words=words, text=" ".join(segment.text[:1000].split())[:240]))
            except AttributeError as exc:
                raise MomentDetectionError("Segmento de voz malformado.") from exc
    check()
    missing = tuple(family for family in _FAMILIES if family not in available)
    if missing:
        warn("Fuentes no disponibles: " + ", ".join(missing) + ". Se usan sólo fuentes completadas.")
    if len(available) < config.min_sources:
        warn(f"Cobertura insuficiente: se requieren {config.min_sources} familias distintas; una señal aislada no basta.")
    for amount, message in ((outside, "eventos fuera de la línea temporal del video"),
                            (long_audio, "eventos acústicos sostenidos"), (short_speech, "segmentos de voz breves o con pocas palabras")):
        if amount:
            warn(f"Se omitieron {amount} {message}.")
    if audio is not None and transcript is not None and "audio" in available and "transcription" in available and audio.stream_index != transcript.stream_index:
        warn("Audio Analysis y Transcription proceden de pistas distintas; coincidencia temporal no garantiza contenido compartido.")

    by_family = {family: [] for family in _FAMILIES}
    for event in events:
        by_family[event.family].append(event)
    for values in by_family.values():
        values.sort(key=lambda event: (event.start, event.kind, event.index))
    visual_index, speech_index = _Index(by_family["scenes"]), _Index(by_family["transcription"])
    audio_indexes = [_Index([event for event in by_family["audio"] if event.kind == kind])
                     for kind in ("audio_peak", "audio_energy_rise", "audio_energy_drop")]
    anchors = sorted(by_family["scenes"] + by_family["audio"], key=lambda event: (event.start, event.family, event.kind, event.index))
    seeds: list[_Seed] = []
    suppressed = oversized = localized = too_short = 0

    def make_seed(selected: list[_Event], anchor: _Event) -> tuple[_Seed | None, str | None, int]:
        supports: list[_Support] = []
        localized_count = 0
        non_speech = [event for event in selected if event.family != "transcription"]
        other_start = min(max(0.0, event.start) for event in non_speech)
        other_end = max(min(event.end, duration) if duration is not None else event.end for event in non_speech)
        for event in selected:
            first, last = max(0.0, event.start), event.end
            if duration is not None:
                last = min(last, duration)
            # Even a sentence shorter than max_duration can make the combined
            # support too long. Localize activity around the observed event.
            if event.family == "transcription" and max(last, other_end) - min(first, other_start) > config.max_duration:
                width = min(config.max_duration, max(config.min_speech_duration, config.coincidence_window * 2))
                left = max(first, min(anchor.start - width / 2, last - width))
                first, last = left, min(last, left + width)
                localized_count += 1
            supports.append(_Support(event, first, last))
        core_start, core_end = min(s.start for s in supports), max(s.end for s in supports)
        if core_end - core_start > config.max_duration + 1e-9:
            return None, "oversized", 0
        first = max(0.0, _number(core_start - config.context_before, "candidate.context_start"))
        last = _number(core_end + config.context_after, "candidate.context_end")
        if duration is not None:
            last = min(last, duration)
        if last - first > config.max_duration:
            first = max(first, last - config.max_duration)
            if first > core_start:
                first, last = core_start, _number(core_start + config.max_duration, "candidate.end")
        if last - first < config.min_duration:
            first = max(0.0, first - (config.min_duration - (last - first)) / 2)
            last = max(last, _number(first + config.min_duration, "candidate.min_end"))
            if duration is not None and last > duration:
                last, first = duration, max(0.0, duration - config.min_duration)
        if last - first < config.min_duration - 1e-9:
            return None, "too_short", 0
        return _Seed(first, last, tuple(supports)), None, localized_count

    for anchor in anchors:
        check()
        if len(available) < config.min_sources:
            break
        options = {"scenes": visual_index.near(anchor, config.coincidence_window),
                   "audio": tuple(event for index in audio_indexes for event in index.near(anchor, config.coincidence_window)),
                   "transcription": speech_index.near(anchor, config.coincidence_window)}
        other = [family for family in _FAMILIES if family != anchor.family]
        triples = [(anchor, first, second) for first in options[other[0]] for second in options[other[1]]
                   if _distance(first, second) <= config.coincidence_window + 1e-9]
        combinations = triples
        if config.min_sources == 2:
            combinations += [(anchor, event) for family in other for event in options[family]]
        if not combinations:
            continue
        combinations.sort(key=lambda group: (-len(group),
            sum(_distance(anchor, event) for event in group),
            tuple((event.start, event.family, event.kind, event.index) for event in group)))
        failure = None
        for combination in combinations:
            selected = list(combination)
            acoustic = next((event for event in selected if event.family == "audio"), None)
            if acoustic is not None:
                for event in options["audio"]:
                    if event.key not in {item.key for item in selected} and abs(event.start - acoustic.start) <= 1e-9 and all(
                            _distance(event, item) <= config.coincidence_window + 1e-9 for item in selected):
                        # Additional same-family evidence must not erase a
                        # viable combination when the configured interval is short.
                        extra = selected + [event]
                        non_speech = [item for item in extra if item.family != "transcription"]
                        if max(item.end for item in non_speech) - min(item.start for item in non_speech) <= config.max_duration:
                            selected.append(event)
            seed, failure, localized_count = make_seed(selected, anchor)
            if seed is not None:
                seeds.append(seed)
                localized += localized_count
                break
        else:
            if failure == "oversized":
                oversized += 1
            else:
                too_short += 1
    check()
    seeds.sort(key=lambda seed: (seed.start, seed.end, tuple(s.event.key for s in seed.supports)))
    moments: list[MomentCandidate] = []
    group_start = group_end = 0.0
    group: dict[tuple[str, int], _Support] = {}
    repetition = capped = evidence_capped = overlaps = 0

    def publish() -> None:
        nonlocal suppressed, repetition, capped, overlaps
        if not group:
            return
        if moments and group_start < moments[-1].end - 1e-9:
            suppressed += 1
            overlaps += 1
            return
        if moments and group_start < moments[-1].start + config.cooldown - 1e-9:
            suppressed += 1
            repetition += 1
            return
        if len(moments) >= config.max_candidates:
            suppressed += 1
            capped += 1
            return
        evidence = tuple(_evidence(support) for support in sorted(group.values(), key=lambda s: (
            s.start, s.event.family, s.event.kind, s.event.index)))
        moments.append(MomentCandidate(group_start, group_end, group_end - group_start,
                                       tuple(sorted({item.kind for item in evidence})), evidence))

    for seed in seeds:
        check()
        if group and seed.start <= group_end + config.merge_gap + 1e-9 and max(group_end, seed.end) - group_start <= config.max_duration + 1e-9:
            new_keys = {s.event.key for s in seed.supports} - group.keys()
            suppressed += 1
            if len(group) + len(new_keys) > _EVIDENCE_LIMIT:
                evidence_capped += 1
                continue
            group_end = max(group_end, seed.end)
            for support in seed.supports:
                existing = group.get(support.event.key)
                if existing is not None:
                    support = _Support(support.event, min(existing.start, support.start), max(existing.end, support.end))
                group[support.event.key] = support
        else:
            publish()
            group_start, group_end = seed.start, seed.end
            group = {s.event.key: s for s in seed.supports}
    publish()
    for amount, message in ((repetition, f"candidatos por cooldown ({config.cooldown:g} s entre inicios)"),
                            (overlaps, "candidatos solapados cuya fusión excedería max_duration; se conserva el primero cronológicamente"),
                            (capped, "candidatos adicionales por max_candidates; se conservan los primeros cronológicamente, sin ranking"),
                            (evidence_capped, f"coincidencias repetitivas por el límite de {_EVIDENCE_LIMIT} evidencias por candidato"),
                            (oversized, "coincidencias cuyo soporte excede max_duration"),
                            (too_short, "coincidencias porque el video no permite min_duration")):
        if amount:
            warn(f"Se suprimieron {amount} {message}.")
    if localized:
        warn("Voz prolongada localizada alrededor de coincidencias; texto como contexto del segmento original, sin alineación de palabras.")
    check()
    return MomentDetectionResult(source, "PASS", tuple(moments), tuple(warnings), tuple(available),
                                 missing, config, monotonic() - started, count,
                                 suppressed + oversized + too_short, origin, duration)
