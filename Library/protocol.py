"""
protocol.py — SmartMart 서버/Qt 클라이언트가 공유하는 주문 상태 상수

값(문자열)은 Service/DB/Schema.SQL 의 orders.status ENUM 과 반드시 똑같아야 한다.
여기서 어긋나면 MySQL이 저장을 거부하거나(strict mode) 빈 문자열로 잘라버린다.
"""


class OrderStatus:
    PENDING = "대기"          # 주문 생성, 결제 전
    PAID = "결제완료"          # 결제 완료, 출고 대기열에 들어감
    DISPATCHING = "출고중"     # 분배 보드에 출고 지시함
    PICKUP_READY = "픽업대기"   # 출고 완료, 픽업박스에서 대기 중
    DONE = "완료"              # 손님이 픽업박스에서 찾아감
    ERROR = "오류"             # 출고 실패/보드 무응답 등. Schema.SQL ENUM에는 아직 없음
