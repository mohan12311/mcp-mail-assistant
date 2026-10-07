import re

def is_newsletter(msg, body_text: str) -> bool:
    # 1) 헤더 기반(가장 강력)
    if msg.get("List-Unsubscribe"):
        return True

    prec = (msg.get("Precedence") or "").lower()
    if prec in ("bulk", "list", "junk"):
        return True

    # 2) 본문 키워드 기반(보조)
    t = (body_text or "").lower()
    keywords = [
        "unsubscribe", "配信停止", "配信解除", "メルマガ", "ニュースレター",
        "このメールは", "advertisement", "広告"
    ]
    return any(k.lower() in t for k in keywords)

def strip_reply_and_signature(text: str) -> str:
    if not text:
        return ""

    # 흔한 "이전 메일 인용" 시작 패턴들
    patterns = [
        r"^from:\s.*$",
        r"^sent:\s.*$",
        r"^to:\s.*$",
        r"^subject:\s.*$",
        r"^-----\s*original message\s*-----$",
        r"^_{5,}$",
        r"^>{1,}.*$",  # '>' 인용 라인(간단 처리)
    ]

    lines = text.splitlines()
    cleaned = []
    for line in lines:
        # 여기서 끊기 시작하면 그 아래는 과거 체인으로 보고 중단
        if any(re.match(p, line.strip(), flags=re.IGNORECASE) for p in patterns):
            break
        cleaned.append(line)

    # 마지막에 남는 서명/구분선도 대충 컷
    out = "\n".join(cleaned).strip()
    out = re.split(r"\n-{2,}\n", out)[0].strip()  # "----" 구분선 아래 컷(느슨)

    for marker in ["\nFrom:", " From:", "\nSent:", " Sent:"]:
        idx = out.find(marker)
        if idx != -1:
            out = out[:idx].strip()
            break

    return out
