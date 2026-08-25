"""
protocol.py — SmartMart 서버/Qt 클라이언트/제어 보드가 공유하는 프로토콜 정의

전송 방식이 구간마다 다르다:
  Qt  ↔ 서버 (:9000)   UTF-8 JSON 한 줄 + '\n'
  픽업 보드 ↔ 서버      UTF-8 JSON 한 줄 + '\n' (USB Serial)
  분배 보드 ↔ 서버 (:9002)  ★ 바이너리 프레임 (아래 참조)
  카메라 → 서버 (UDP)   바이너리 청크 (UDPModule.py)

OrderStatus 의 값(문자열)은 Service/DB/Schema.SQL 의 orders.status ENUM 과
반드시 똑같아야 한다. 여기서 어긋나면 MySQL이 저장을 거부하거나(strict mode)
빈 문자열로 잘라버린다.

★ enum.Enum 이 아니라 문자열 상수 클래스인 이유:
  이 값들은 그대로 JSON 에 실려 나가고 DB 에 저장된다. Enum 이면 json.dumps 가
  거부해서 어디서든 .value 를 붙여야 하고, 한 군데만 빠뜨려도 런타임에 터진다.
  문자열 상수는 그냥 문자열이라 비교/직렬화/DB 저장이 전부 그대로 된다.
  회선에 나가는 1바이트 코드는 따로 FAIL_REASON_CODE 표로 옮긴다.
"""

import struct


class OrderStatus:
    PENDING = "대기"          # 주문 생성, 결제 전
    PAID = "결제완료"          # 결제 완료, 출고 대기열에 들어감
    DISPATCHING = "출고중"     # 분배 보드에 출고 지시함
    PICKUP_READY = "픽업대기"   # 출고 완료, 픽업박스에서 대기 중
    DONE = "완료"              # 손님이 픽업박스에서 찾아감
    ERROR = "오류"             # 출고 실패/보드 무응답 등


class FailReason:
    """orderFailed 의 reason 값. 관리자 화면 알림 문구에 그대로 실린다.

    보드가 보내는 것과 서버가 스스로 만드는 것이 섞여 있는데, 둘 다
    MainService._onOrderFailed 로 들어와 같은 길로 처리된다.
    회선에는 문자열이 아니라 1바이트 코드로 나간다(FAIL_REASON_CODE).
    """

    JAM = "jam"                     # (보드) 출구 센서가 jamTimeout 안에 통과를 못 셈
    BOARD_TIMEOUT = "boardTimeout"  # (서버) 출고 지시 후 ORDER_TIMEOUT 동안 무응답
    BOARD_RESET = "boardReset"      # (서버) 주문 처리 중 보드가 끊기거나 재접속함
    UNKNOWN = "unknown"             # (서버) reason 필드가 아예 없을 때의 기본값
    BUSY = "busy"                   # (보드) orderRejected 전용 — 다른 주문 처리 중


# 문자열 ↔ 1바이트 코드. 펌웨어의 상수와 값이 같아야 한다.
FAIL_REASON_CODE = {
    FailReason.UNKNOWN: 0x00,
    FailReason.JAM: 0x01,
    FailReason.BOARD_TIMEOUT: 0x02,
    FailReason.BOARD_RESET: 0x03,
    FailReason.BUSY: 0x04,
}
FAIL_REASON_NAME = {code: name for name, code in FAIL_REASON_CODE.items()}


# ══════════════════════════════════════════════════════════════════
# 분배 보드(WiFi :9002) 바이너리 프레임
# ══════════════════════════════════════════════════════════════════
#
#   [ TAG (ASCII 2바이트) ][ PAYLOAD (태그마다 길이 고정) ]
#
# 길이 필드가 따로 없다. 태그를 읽으면 뒤에 몇 바이트가 오는지 PAYLOAD_LEN
# 으로 알 수 있기 때문이다. 그래서 수신 쪽은 "2바이트 읽고 → 표에서 길이를
# 찾고 → 그만큼 더 읽는다" 만 반복하면 된다.
#
# 숫자는 전부 빅엔디안(네트워크 바이트 순서).
#   orderId   uint16   1 ~ 65535
#   counts    uint8 ×3  DISPENSER_PRODUCTS 순서의 개수
#   dispensed uint8 ×3  실제로 배출된 개수
#   slot      uint8     픽업박스 번호 (1부터)
#   reason    uint8     FAIL_REASON_CODE
#
#   서버 → 보드
#     SO  startOrder      orderId(2) counts(3) slot(1)          = 6
#   보드 → 서버
#     HL  hello           (없음)                                 = 0
#     OC  orderComplete   orderId(2) dispensed(3)                = 5
#     OF  orderFailed     orderId(2) dispensed(3) reason(1)      = 6
#     OR  orderRejected   orderId(2) reason(1)                   = 3

CMD_HELLO = b"HL"
CMD_START_ORDER = b"SO"
CMD_ORDER_COMPLETE = b"OC"
CMD_ORDER_FAILED = b"OF"
CMD_ORDER_REJECTED = b"OR"

PAYLOAD_LEN = {
    CMD_HELLO: 0,
    CMD_START_ORDER: 6,
    CMD_ORDER_COMPLETE: 5,
    CMD_ORDER_FAILED: 6,
    CMD_ORDER_REJECTED: 3,
}

TAG_SIZE = 2
MAX_ORDER_ID = 0xFFFF       # orderId 가 uint16 이라 여기까지만 실린다

_START_ORDER = struct.Struct(">HBBBB")   # orderId, c0, c1, c2, slot
_ORDER_COMPLETE = struct.Struct(">HBBB")  # orderId, d0, d1, d2
_ORDER_FAILED = struct.Struct(">HBBBB")   # orderId, d0, d1, d2, reason
_ORDER_REJECTED = struct.Struct(">HB")    # orderId, reason


def _byte(value) -> int:
    """uint8 한 칸에 안전하게 넣는다. 개수가 255를 넘을 일은 없지만 잘라둔다."""
    try:
        return max(0, min(255, int(value)))
    except (TypeError, ValueError):
        return 0


def _counts3(value) -> list[int]:
    """길이가 모자라거나 남아도 항상 3칸으로 맞춘다."""
    items = list(value or [])[:3]
    items += [0] * (3 - len(items))
    return [_byte(v) for v in items]


def encodeBoardFrame(obj: dict) -> bytes:
    """서버가 쓰는 dict → 회선에 나갈 바이트.

    dict 형태를 그대로 두는 이유: MainService 도 관리자 통신 로그도 이미
    dict 로 말하고 있다. 바이트로 바꾸는 일은 네트워크 경계(BoardHub)에서만
    일어나고, 그 위쪽 코드는 한 줄도 바뀌지 않는다.
    """
    cmd = obj.get("cmd")
    if cmd == "startOrder":
        orderId = int(obj["orderId"])
        if not 0 <= orderId <= MAX_ORDER_ID:
            raise ValueError(f"orderId {orderId} 가 uint16 범위를 넘음")
        c0, c1, c2 = _counts3(obj.get("counts"))
        return CMD_START_ORDER + _START_ORDER.pack(
            orderId, c0, c1, c2, _byte(obj.get("slot")))
    raise ValueError(f"바이너리로 보낼 수 없는 명령: {cmd}")


def decodeBoardFrame(tag: bytes, payload: bytes, boardName: str) -> dict | None:
    """회선에서 읽은 프레임 → 서버가 쓰는 dict. 모르는 태그면 None.

    boardName 은 HL(hello) 에 이름 필드가 없어서 받는다. :9002 에 붙는
    보드가 분배 보드 하나뿐이라 프레임에서 뺀 값이다.
    """
    if tag == CMD_HELLO:
        return {"hello": boardName}

    if tag == CMD_ORDER_COMPLETE:
        orderId, d0, d1, d2 = _ORDER_COMPLETE.unpack(payload)
        return {"event": "orderComplete", "orderId": orderId,
                "dispensed": [d0, d1, d2]}

    if tag == CMD_ORDER_FAILED:
        orderId, d0, d1, d2, reason = _ORDER_FAILED.unpack(payload)
        return {"event": "orderFailed", "orderId": orderId,
                "dispensed": [d0, d1, d2],
                "reason": FAIL_REASON_NAME.get(reason, FailReason.UNKNOWN)}

    if tag == CMD_ORDER_REJECTED:
        orderId, reason = _ORDER_REJECTED.unpack(payload)
        return {"event": "orderRejected", "orderId": orderId,
                "reason": FAIL_REASON_NAME.get(reason, FailReason.UNKNOWN)}

    return None
