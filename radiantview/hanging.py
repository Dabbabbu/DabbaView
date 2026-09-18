"""
Hanging Protocol: 검사의 모달리티/부위에 맞춰 레이아웃과 시리즈 배치를 결정

프로토콜 dict:
  name, modality('MR' 등, 빈 값이면 무관), body('BRAIN|HEAD' 등 키워드, 빈 값이면 무관),
  layout('2x2' 등), slots(칸 순서대로 SeriesDescription 키워드)
"""
import re


def _tokens(text):
    return [t for t in re.split(r"[\s_\-/]+", (text or "").upper()) if t]


def _study_text(series_list):
    """부위 판정에 쓰는 문자열: StudyDescription, BodyPartExamined, 시리즈 설명"""
    parts = []
    for s in series_list:
        ds = s.slices[0] if s.slices else None
        if ds is None:
            continue
        for key in ("StudyDescription", "BodyPartExamined", "ProtocolName"):
            parts.append(str(getattr(ds, key, "") or ""))
        parts.append(s.description or "")
    return " ".join(parts).upper()


def protocol_matches(protocol, series_list):
    if not series_list:
        return False
    modality = (protocol.get("modality") or "").strip().upper()
    if modality and not any((s.modality or "").upper() == modality for s in series_list):
        return False
    body = (protocol.get("body") or "").strip()
    if body:
        text = _study_text(series_list)
        if not any(k.strip().upper() in text for k in body.split("|") if k.strip()):
            return False
    return True


def find_protocol(protocols, series_list):
    """첫 번째로 맞는 프로토콜 (없으면 None)"""
    for protocol in protocols:
        if protocol_matches(protocol, series_list):
            return protocol
    return None


def slot_score(keyword, description):
    """키워드 토큰이 모두 설명에 있으면 점수(남는 토큰이 적을수록 높음), 아니면 None"""
    want = _tokens(keyword)
    have = _tokens(description)
    if not want or not all(any(w == h or (len(w) > 2 and w in h) for h in have)
                           for w in want):
        return None
    return 100 - (len(have) - len(want))


def assign_slots(protocol, series_list):
    """칸별 시리즈 목록 (못 찾은 칸은 None). 한 시리즈는 한 칸에만

    다른 칸 키워드에도 맞는 시리즈는 감점 (예: 'T2' 칸에는 'T2 FLAIR'보다 'T2 FSE').
    점수 높은 (칸, 시리즈) 쌍부터 전역으로 배정.
    """
    slots = protocol.get("slots", [])
    pairs = []
    for i, keyword in enumerate(slots):
        for order, s in enumerate(series_list):
            score = slot_score(keyword, s.description)
            if score is None:
                continue
            others = sum(1 for j, k in enumerate(slots)
                         if j != i and k and slot_score(k, s.description) is not None)
            pairs.append((score - 10 * others, -order, i, s))
    pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
    result = [None] * len(slots)
    used = set()
    for _, _, i, s in pairs:
        if result[i] is None and s.series_uid not in used:
            result[i] = s
            used.add(s.series_uid)
    return result


def protocol_from_layout(name, layout, series_in_slots, series_list):
    """현재 Multi View 배치로 사용자 프로토콜 생성"""
    modalities = sorted({s.modality for s in series_list if s.modality})
    body = ""
    for s in series_list:
        ds = s.slices[0] if s.slices else None
        part = str(getattr(ds, "BodyPartExamined", "") or "").strip() if ds else ""
        if part:
            body = part.upper()
            break
    return {
        "name": name,
        "modality": modalities[0] if len(modalities) == 1 else "",
        "body": body,
        "layout": layout,
        "slots": [s.description if s else "" for s in series_in_slots],
    }
