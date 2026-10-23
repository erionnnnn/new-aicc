"""현재 질문 상태에 한정한 발화 의도 분류(규칙 기반, 생성형 AI 미사용).

우선순위(설계 05): 연락거부·종료 → 잘못된 상대 → (끼어들기는 상태기계가 처리) → 현재 질문의 답 → 예외 질문.
단순 '네'는 현재 질문에 대한 동의로만 해석한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .dates import DateMention, extract_dates

# 영구 연락거부: 앞으로 연락하지 말라는 명시적 요청
_REFUSAL = re.compile(
    r"(연락|전화)\s*(?:하지\s*마|하지마|마세요|말아|좀\s*그만|그만\s*하|안\s*했으면|하지\s*말|다시는\s*하지)"
    r"|다시는\s*(?:연락|전화)|번호\s*(?:좀\s*)?(?:지워|삭제)|수신\s*거부|연락\s*거부|전화\s*거부"
    r"|더\s*이상\s*(?:연락|전화)|(?:연락|전화)\s*받기\s*싫"
)
# 지금 통화 곤란(영구 거부 아님) — '오늘 안 돼요'는 영구 거부로 단정하지 않는다(설계 11)
_BUSY = re.compile(
    r"지금\s*(?:은\s*)?(?:바빠|바쁘|통화\s*(?:어려|곤란|못|안\s*돼|안돼|힘들)|운전|일하는\s*중|근무\s*중이라)"
    r"|나중에\s*(?:전화|통화|연락)|이따가?\s*(?:전화|통화|연락)|통화\s*(?:어려워|곤란|힘들|못\s*해)|바빠요|바쁩니다"
    r"|오늘\s*(?:은\s*)?(?:통화\s*)?안\s*돼|회의\s*중|끊을게|끊겠"
)
_WRONG = re.compile(
    r"잘못\s*(?:거|걸|연락)|번호\s*잘못|그런\s*사람\s*없|그런\s*분\s*없|그런\s*분\s*아니|저\s*아닌데|제가\s*아닌데"
    r"|모르는\s*사람|간병\s*(?:안\s*해요|안\s*합니다|일\s*안\s*해)|간병인\s*아니"
)
_VOICEMAIL = re.compile(
    r"음성\s*사서함|삐\s*소리|연결이\s*되지\s*않|전화를\s*받을\s*수\s*없|고객님의?\s*전화|녹음\s*후|"
    r"전원이\s*꺼져|없는\s*번호|결번|통화중이오니"
)
_YES = re.compile(
    r"^(?:네+|넵|넹|예+|응+|어+|그래요|그렇죠|그렇습니다|맞아요?|맞습니다|맞네요|맞죠|좋아요|괜찮아요|괜찮습니다|됩니다|돼요|되요|"
    r"그럼요|물론이죠|오케이|ok|okay|yes)\b"
    r"|^(?:네|예)[,.\s]"
    r"|맞아요|맞습니다|맞네요|그렇습니다|그래요\s*$|괜찮아요|괜찮습니다|네\s*괜찮|네\s*맞"
)
_NO = re.compile(
    r"^(?:아니+요?|아뇨|아니에요|아닙니다|아니오|노|no|안\s*돼요|안돼요|틀려요|틀렸어요)"
    r"|아니(?:요|에요|오|야)?[,.\s]|아닌데|틀렸|그게\s*아니|아니라"
)
_WORKING = re.compile(
    r"(?:지금|현재|요즘)?\s*(?:근무|일|간병)\s*(?:하고\s*있|중이|하는\s*중|나가고\s*있|다니고\s*있)"
    r"|일하고\s*있|근무\s*중|간병\s*중|현장\s*(?:에\s*)?있|병원에\s*있|일하는\s*중"
)
_NOT_WORKING = re.compile(
    r"(?:지금|현재|요즘)?\s*(?:쉬고\s*있|쉬는\s*중|일\s*안\s*하|근무\s*안\s*하|놀고\s*있|대기\s*중|끝났|그만뒀|"
    r"쉬어요|쉽니다|일\s*없|안\s*나가|집에\s*있)"
)
_QUESTION_HINT = re.compile(
    r"\?|누구세요|누구시|어디(?:세요|라고|서|에서)|무슨\s*일|왜\s*(?:요|그러|물어|전화)|뭐(?:예요|에요|라고|하는|죠)|"
    r"어떻게|얼마|급여|일당|시급|돈|병원|환자|어느\s*지역|위치|조건|무슨\s*회사|회사\s*이름|녹음|ai|에이아이|로봇|기계|"
    r"사람이에요|사람\s*맞|실장님\s*(?:이세요|맞)|전담|배정|일자리|소개|연결해"
)
_REPEAT = re.compile(r"뭐라고|다시\s*(?:한\s*번|말|얘기)|잘\s*안\s*들|안\s*들려|못\s*들었|네\?$|예\?$")

INTENTS = (
    "REFUSAL", "BUSY", "WRONG_PERSON", "VOICEMAIL", "REPEAT", "YES", "NO", "WORKING", "NOT_WORKING",
    "DATE", "ANYTIME", "UNKNOWN_DATE", "RANGE", "QUESTION", "UNCLEAR",
)


@dataclass(frozen=True)
class Interpretation:
    intent: str
    dates: tuple[DateMention, ...]
    yes: bool
    no: bool
    text: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def interpret(text: str, state: str, reference: date) -> Interpretation:
    """state: 현재 질문 상태. 반환 의도는 상태기계가 상태에 맞게 다시 판단한다."""
    t = _clean(text)
    mentions = tuple(extract_dates(t, reference))
    yes = bool(_YES.search(t))
    no = bool(_NO.search(t))

    def r(intent: str) -> Interpretation:
        return Interpretation(intent, mentions, yes, no, text)

    if not t:
        return r("UNCLEAR")
    if _VOICEMAIL.search(t):
        return r("VOICEMAIL")
    if _REFUSAL.search(t):
        return r("REFUSAL")
    if _WRONG.search(t):
        return r("WRONG_PERSON")
    if _REPEAT.search(t) and not mentions:
        return r("REPEAT")

    has_date = any(m.kind == "DATE" for m in mentions)
    has_range = any(m.kind == "RANGE" for m in mentions)
    has_any = any(m.kind == "ANYTIME" for m in mentions)
    has_unknown = any(m.kind == "UNKNOWN" for m in mentions)

    # 날짜 정보가 있는 바쁨 표현("오늘은 안 돼요, 내일부터 가능")은 날짜 답으로 우선한다.
    # '오늘은 안 돼요'의 '오늘'은 날짜 답이 아니라 통화 곤란 표현일 수 있다.
    informative_date = any(
        m.kind == "DATE" and (m.role == "START" or m.source.strip() != "오늘") for m in mentions
    )
    if _BUSY.search(t) and not informative_date:
        return r("BUSY")

    if state == "CONFIRM_DATE":
        # 날짜가 함께 나오면 상태기계가 후보와 비교해 동의/정정을 판단한다.
        if has_date:
            return r("DATE")
        if no:
            return r("NO")
        if yes:
            return r("YES")
    if state in ("ASK_WORKING", "OPENING"):
        if _NOT_WORKING.search(t):
            return r("NOT_WORKING")
        if _WORKING.search(t):
            return r("WORKING")
    if state == "ASK_WORKING":
        if has_date and any(m.role == "END" for m in mentions):
            return r("WORKING")
        if no:
            return r("NOT_WORKING")
        if yes:
            return r("WORKING")
    if state == "OPENING":
        if no:
            return r("NO")
        if yes:
            return r("YES")

    if has_date:
        return r("DATE")
    if has_any:
        return r("ANYTIME")
    if has_range:
        return r("RANGE")
    if has_unknown:
        return r("UNKNOWN_DATE")
    if _QUESTION_HINT.search(t):
        return r("QUESTION")
    if yes:
        return r("YES")
    if no:
        return r("NO")
    return r("UNCLEAR")
