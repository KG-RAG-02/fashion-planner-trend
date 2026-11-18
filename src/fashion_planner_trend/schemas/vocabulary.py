"""표준 어휘 (K-Fashion 기반). 문서: docs/vocabulary.md

Enum 값(한글)이 JSON에 들어가는 문자열이다. 멤버 이름은 문서의 표와 같다.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Color(str, Enum):
    BLACK = "블랙"
    WHITE = "화이트"
    GREY = "그레이"
    RED = "레드"
    PINK = "핑크"
    ORANGE = "오렌지"
    BEIGE = "베이지"
    BROWN = "브라운"
    YELLOW = "옐로우"
    GREEN = "그린"
    KHAKI = "카키"
    MINT = "민트"
    BLUE = "블루"
    NAVY = "네이비"
    SKYBLUE = "스카이블루"
    PURPLE = "퍼플"
    LAVENDER = "라벤더"
    WINE = "와인"
    NEON = "네온"
    GOLD = "골드"
    SILVER = "실버"


class CategoryMajor(str, Enum):
    UPPER = "상의"
    LOWER = "하의"
    OUTER = "아우터"
    ONEPIECE = "원피스"


class CategoryMinor(str, Enum):
    # 상의
    TOP = "탑"
    BLOUSE = "블라우스"
    TSHIRT = "티셔츠"
    KNITWEAR = "니트웨어"
    SHIRT = "셔츠"
    BRA_TOP = "브라탑"
    HOODIE = "후드티"
    # 하의
    JEANS = "청바지"
    PANTS = "팬츠"
    SKIRT = "스커트"
    LEGGINGS = "레깅스"
    JOGGER_PANTS = "조거팬츠"
    # 아우터
    COAT = "코트"
    JACKET = "재킷"
    JUMPER = "점퍼"
    PADDING = "패딩"
    VEST = "베스트"
    CARDIGAN = "가디건"
    ZIP_UP = "짚업"
    # 원피스
    DRESS = "드레스"
    JUMPSUIT = "점프수트"


MINOR_TO_MAJOR: dict[CategoryMinor, CategoryMajor] = {
    **{m: CategoryMajor.UPPER for m in (
        CategoryMinor.TOP, CategoryMinor.BLOUSE, CategoryMinor.TSHIRT, CategoryMinor.KNITWEAR,
        CategoryMinor.SHIRT, CategoryMinor.BRA_TOP, CategoryMinor.HOODIE)},
    **{m: CategoryMajor.LOWER for m in (
        CategoryMinor.JEANS, CategoryMinor.PANTS, CategoryMinor.SKIRT, CategoryMinor.LEGGINGS,
        CategoryMinor.JOGGER_PANTS)},
    **{m: CategoryMajor.OUTER for m in (
        CategoryMinor.COAT, CategoryMinor.JACKET, CategoryMinor.JUMPER, CategoryMinor.PADDING,
        CategoryMinor.VEST, CategoryMinor.CARDIGAN, CategoryMinor.ZIP_UP)},
    **{m: CategoryMajor.ONEPIECE for m in (CategoryMinor.DRESS, CategoryMinor.JUMPSUIT)},
}


class Material(str, Enum):
    FUR = "퍼"
    MOUTON = "무스탕"
    SUEDE = "스웨이드"
    ANGORA = "앙고라"
    CORDUROY = "코듀로이"
    SEQUIN_GLITTER = "시퀸/글리터"
    DENIM = "데님"
    JERSEY = "저지"
    TWEED = "트위드"
    VELVET = "벨벳"
    VINYL_PVC = "비닐/PVC"
    WOOL_CASHMERE = "울/캐시미어"
    SYNTHETIC = "합성섬유"
    KNIT = "니트"
    LACE = "레이스"
    LINEN = "린넨"
    MESH = "메시"
    FLEECE = "플리스"
    NEOPRENE = "네오프렌"
    SILK = "실크"
    SPANDEX = "스판덱스"
    JACQUARD = "자카드"
    LEATHER = "가죽"
    COTTON = "면"
    CHIFFON = "시폰"
    HAIR_KNIT = "헤어니트"


class Pattern(str, Enum):
    CHECK = "체크"
    STRIPE = "스트라이프"
    ZIGZAG = "지그재그"
    LEOPARD = "호피"
    ZEBRA = "지브라"
    DOT = "도트"
    CAMOUFLAGE = "카무플라쥬"
    PAISLEY = "페이즐리"
    ARGYLE = "아가일"
    FLORAL = "플로럴"
    LETTERING = "레터링"
    SKULL = "해골"
    TIE_DYE = "타이다이"
    GRADATION = "그라데이션"
    SOLID = "무지"
    GRAPHIC = "그래픽"
    HOUNDSTOOTH = "하운즈투스"
    GINGHAM = "깅엄"
    MIX = "믹스"


class DetailName(str, Enum):
    BEADS = "비즈"
    BUTTONED = "단추"
    CABLE = "니트꽈배기"
    CHAIN = "체인"
    CUT_OFF = "컷오프"
    DOUBLE_BREASTED = "더블브레스티드"
    DROP_SHOULDER = "드롭숄더"
    EMBROIDERY = "자수"
    FRILL = "프릴"
    FRINGE = "프린지"
    FLARE = "플레어"
    QUILTING = "퀼팅"
    RIBBON = "리본"
    ROLL_UP = "롤업"
    RUFFLE = "러플"
    SHIRRING = "셔링"
    SLIT = "슬릿"
    SPANGLE = "스팽글"
    STITCH = "스티치"
    STUD = "스터드"
    FUR_TRIMMED = "퍼트리밍"
    GLITTER = "글리터"
    HOOD = "후드"
    LACE_UP = "레이스업"
    PADDING = "패딩"
    PATCHWORK = "패치워크"
    PEPLUM = "페플럼"
    PLEATS = "플리츠"
    POCKETS = "포켓"
    POMPOM = "폼폼"
    PUFF = "퍼프"
    TASSEL = "태슬"
    ZIP_UP = "집업"
    SASH = "띠"
    DESTROYED = "디스트로이드"
    LOW_WAIST = "드롭웨이스트"
    BUCKLE = "버클"
    CUT_OUT = "컷아웃"
    X_STRAP = "X스트랩"
    UNBALANCED = "비대칭"


class Neckline(str, Enum):
    ROUND = "라운드넥"
    U = "유넥"
    V = "브이넥"
    HALTER = "홀터넥"
    OFF_SHOULDER = "오프숄더"
    ONE_SHOULDER = "원숄더"
    SQUARE = "스퀘어넥"
    COLLARLESS = "노카라"
    HOOD = "후드"
    TURTLENECK = "터틀넥"
    BOAT = "보트넥"
    SWEETHEART = "스위트하트"


class Collar(str, Enum):
    SHIRT = "셔츠칼라"
    BOW = "보우칼라"
    SAILOR = "세일러칼라"
    SHAWL = "숄칼라"
    POLO = "폴로칼라"
    PETERPAN = "피터팬칼라"
    NOTCHED = "너치드칼라"
    STANDUP = "차이나칼라"
    TAILORED = "테일러드칼라"
    BAND = "밴드칼라"


class DetailType(str, Enum):
    DETAIL = "detail"
    NECKLINE = "neckline"
    COLLAR = "collar"


DETAIL_ENUMS: dict[DetailType, type[Enum]] = {
    DetailType.DETAIL: DetailName,
    DetailType.NECKLINE: Neckline,
    DetailType.COLLAR: Collar,
}


def detail_node_id(detail_type: DetailType, value: str) -> str:
    """Detail 노드 id = "<type>:<name>" (후드는 디테일과 넥라인에 모두 있다)."""
    return f"{DetailType(detail_type).value}:{value}"


class BodyLength(str, Enum):
    CROP = "크롭"
    NORMAL = "노멀"
    HALF = "하프"
    LONG = "롱"
    MINI = "미니"
    KNEE = "니렝스"
    MIDI = "미디"
    ANKLE = "발목"
    MAXI = "맥시"


class SleeveLength(str, Enum):
    SLEEVELESS = "민소매"
    SHORT = "반팔"
    CAP = "캡"
    THREE_QUARTER = "7부소매"
    LONG = "긴팔"


class Fit(str, Enum):
    NORMAL = "노멀"
    LOOSE = "루즈"
    OVERSIZED = "오버사이즈"
    TIGHT = "타이트"
    SKINNY = "스키니"
    WIDE = "와이드"
    BELL_BOTTOM = "벨보텀"


class Shape(str, Enum):
    PEPLUM = "페플럼"
    MERMAID = "머메이드"
    ASYMMETRICAL = "비대칭"
    BELL_FLARE = "벨보텀/플레어"
    BOOTS_CUT = "부츠컷"
    PENCIL = "펜슬"
    TAPERED = "테이퍼드"
    A_LINE = "A라인"
    STRAIGHT = "스트레이트"
    H_LINE = "H라인"


class Season(str, Enum):
    SS = "SS"
    FW = "FW"


class SeasonRef(BaseModel):
    """트렌드가 유행했다고 기사가 말한 시즌 하나 (예: 2027 SS). 그래프에는 문자열 키("2027SS")로 저장한다."""

    year: int
    season: Season

    @property
    def key(self) -> str:
        return f"{self.year}{self.season.value}"


class Gender(str, Enum):
    MALE = "남"
    FEMALE = "여"
    UNISEX = "공용"


# 점수에 쓰이는 속성 (트렌드 핵심·보조 속성의 허용 값). 문서: docs/architecture.md
class ScoredAttribute(str, Enum):
    CATEGORY = "category"  # 하분류(category_minor) 보너스 α의 배수로 쓰인다 (가중치 속성은 아님)
    COLOR = "color"
    MATERIAL = "material"
    PATTERN = "pattern"
    DETAIL = "detail"
    FIT = "fit"


class FocusLevel(str, Enum):
    CORE = "core"
    SUPPORTING = "supporting"
