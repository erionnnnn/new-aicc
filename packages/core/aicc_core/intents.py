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
    r"(?:연락|전화)\s*(?:는|은|을|를)?\s*(?:하지\s*말|그만)|그만\s*해\s*주|(?:연락|전화)을?\s*원하지\s*않|받지\s*않겠|"
    r"더는?\s*(?:연락|전화)하지|"
    r"(연락|전화)\s*(?:하지\s*마|하지마|마세요|말아|좀\s*그만|그만\s*하|안\s*했으면|하지\s*말|다시는\s*하지)"
    r"|다시는\s*(?:연락|전화)|번호\s*(?:좀\s*)?(?:지워|삭제)|수신\s*거부|연락\s*거부|전화\s*거부"
    r"|더\s*이상\s*(?:연락|전화)|(?:연락|전화)\s*받기\s*싫"
)
# 지금 통화 곤란(영구 거부 아님) — '오늘 안 돼요'는 영구 거부로 단정하지 않는다(설계 11)
_BUSY = re.compile(
    r"지금\s*(?:은\s*)?(?:바빠|바쁘|통화\s*(?:어려|곤란|못|안\s*돼|안돼|힘들)|운전|일하는\s*중|근무\s*중이라)"
    r"|나중에\s*(?:전화|통화|연락)(?!\s*(?:드릴|드리겠|할게|할께|하겠))|이따가?\s*(?:전화|통화|연락)(?!\s*(?:드릴|드리겠|할게|할께|하겠))|통화\s*(?:어려워|곤란|힘들|못\s*해)|바빠요|바쁩니다"
    r"|오늘\s*(?:은\s*)?(?:통화\s*)?안\s*돼|회의\s*중|끊을게|끊겠"
    r"|전화\s*받을\s*상황이?\s*아니|통화\s*(?:가|는)?\s*(?:어려|곤란|힘들)|나중에\s*(?:통화|말씀)|통화하기\s*어렵"
    # 2026-10-29 사용자 예시 '나중에 전화주세요' 계열: 사이에 '다시·좀' 이 끼거나 '걸어', '다음에 해요', '나중에요'
    r"|(?:나중에|이따가?|좀\s*있다가?|다음에|조금\s*뒤에?|잠시\s*뒤에?)\s*(?:좀\s*)?(?:다시\s*)?(?:전화|통화|연락|걸어|해\s*주|해요|하죠|하자|합시다)(?!\s*(?:드릴|드리겠|할게|할께|하겠|할\s*게))"
    r"|^\s*(?:네\W*)?(?:나중에|다음에|이따가?)\s*(?:요|요\.|해요)?\s*[.!?~]*\s*$"
)
_WRONG = re.compile(
    r"찾으시는\s*(?:사람|분)이?\s*아(?:니|닙|닌)|다른\s*분\s*(?:번호|찾)|다른\s*사람\s*(?:번호|찾)|번호를?\s*잘못|잘못\s*누르|"
    r"잘못\s*(?:거|걸|연락)|번호\s*잘못|그런\s*사람\s*없|그런\s*분\s*없|그런\s*분\s*아니|저\s*아닌데|제가\s*아닌데"
    r"|모르는\s*사람|간병\s*(?:안\s*해요|안\s*합니다|일\s*안\s*해)|간병(?:인|사)?\s*(?:가\s*|이\s*)?아니(?:에요|야|예요|요)?(?:\s|$|[.?!,])|간병(?:인|사)?\s*(?:가\s*|이\s*)?아닌데|간병(?:인|사)\s*(?:가\s*|이\s*)?아닙니다"
)
_VOICEMAIL = re.compile(
    r"음성\s*사서함|삐\s*소리|연결이\s*되지\s*않|전화를\s*받을\s*수\s*없|고객님의?\s*전화|녹음\s*후|"
    r"전원이\s*꺼져|없는\s*번호|결번|통화중이오니|통화하실\s*수\s*없|착신이?\s*정지|당분간\s*착신|"
    r"지금\s*거신\s*번호|고객의\s*요청에\s*의해"
)
_YES = re.compile(
    r"^(?:네+|넵|넹|예+|응+|어+|말씀하세요|말씀하시면|듣고\s*있|그래요|그렇죠|그렇습니다|맞아요?|맞습니다|맞네요|맞죠|좋아요|괜찮아요|괜찮습니다|됩니다|돼요|되요|"
    r"그럼요|물론이죠|오케이|ok|okay|yes)\b"
    r"|^(?:네|예)[,.\s]|^(?:말씀하세요|말씀하시면|말씀해\s*주|듣고\s*있)"
    r"|맞아요|맞습니다|맞네요|그렇습니다|그래요\s*$|괜찮아요|괜찮습니다|네\s*괜찮|네\s*맞"
    # 2026-10-29 점검: '아 네네', '간병사 맞는데요', '확실해요'
    r"|^(?:아|어)\s*(?:네|예)+|간병(?:사|인)\s*(?:맞|인데)|^확실(?:해요|합니다)"
)
_NO = re.compile(
    r"^(?:아니+요?|아뇨|아니에요|아닙니다|아니오|노|no|안\s*돼요|안돼요|틀려요|틀렸어요)"
    r"|아니(?:요|에요|오|야)?[,.\s]|아닌데|틀렸|그게\s*아니|아니라"
    r"|그\s*날\s*(?:말고|은\s*안|은\s*아니)|그\s*전에도|더\s*빨리|더\s*일찍"
)
_WORKING = re.compile(
    r"(?:지금|현재|요즘)?\s*(?:근무|일|간병)\s*(?:하고\s*있|중이|하는\s*중|나가고\s*있|다니고\s*있)"
    r"|일하고\s*있|근무\s*중|간병\s*중|현장\s*(?:에\s*)?있|병원에\s*있|일하는\s*중"
    # 2026-10-27 시나리오: '아직 맡은 환자분이 계세요', '병원에서 일하고 있습니다', '일하는 게 끝나야 알 수 있어요' 등
    r"|맡은\s*(?:일|환자)|환자\s*(?:분\s*)?(?:곁|을\s*보고|를\s*보고|간병하고|보고\s*있)|환자분\s*간병|근무하고\s*있"
    r"|(?:일|간병\s*일|다른\s*일)을?\s*하고\s*있|근무가\s*있|일하는\s*(?:게|병원)|입실해서|간병\s*일을\s*하고"
)
_NOT_WORKING = re.compile(
    r"(?:지금|현재|요즘)?\s*(?:쉬고\s*있|쉬는\s*중|일\s*안\s*(?:하|해)|근무\s*안\s*(?:하|해)|놀고\s*있|대기\s*중|끝났|그만뒀|"
    r"쉬어요|쉽니다|일\s*없|안\s*나가|집에\s*있)|아직.{0,4}안\s*(?:하|해)"
)
_QUESTION_HINT = re.compile(
    r"\?|누구세요|누구시|어디(?:세요|라고|서|에서)|무슨\s*일|왜\s*(?:요|그러|물어|전화)|뭐(?:예요|에요|라고|하는|죠)|"
    r"어떻게|얼마|급여|일당|시급|돈|병원|환자|어느\s*지역|위치|조건|무슨\s*회사|회사\s*이름|녹음|ai|에이아이|로봇|기계|"
    r"사람이에요|사람\s*맞|실장님\s*(?:이세요|맞)|전담|배정|일자리|소개|연결해"
)
_REPEAT = re.compile(r"뭐라고|다시\s*(?:한\s*번|말|얘기|설명|부탁)|잘\s*안\s*들|안\s*들려|못\s*들었|네\?$|예\?$"
                     # 2026-10-29: 날짜 확인 질문에 '네? 다시요' 가 '네'(동의)로 확정되던 위험 → 다시 묻기
                     r"|^(?:네|예)\s*\?|다시\s*(?:한\s*번\s*)?요\s*[.?!~]*\s*$|^(?:뭐|뭣|뭔)\s*라고"
                     r"|소리가?\s*작|시끄러워서|끊겨서|한\s*번만\s*다시|잘\s*못\s*들")

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
    t = re.sub(r"\s+", " ", text.strip().lower())
    # '여보세요, 네' '여보세요. 말씀하세요' — 앞의 여보세요는 떼고 판단(뒤에 말이 있을 때만)
    return re.sub(r"^(?:여보세요|안녕하세요)[,.!?\s]+(?=\S)", "", t)


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
    if state in ("ASK_WORKING", "OPENING", "WAIT_AVAILABILITY", "ASK_AVAILABLE_DATE") and not (
            state == "ASK_AVAILABLE_DATE" and any(m.kind == "DATE" and m.role == "START" for m in mentions)):
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
