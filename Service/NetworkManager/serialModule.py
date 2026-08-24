"""
serialModule.py
NetworkManager가 사용하는 Serial 통신 모듈 — 픽업보드 전용

프로토콜: JSON 한 줄 + 개행문자('\\n')
    보드 -> 서버: {"hello":"pickup"}                          (접속 시 1회)
    보드 -> 서버: {"event":"slotState","boardId":"pickup","slot":1,"occupied":true}
"""

import json
import threading
import time

try:
    import serial
except ImportError:
    serial = None


class SerialModule:
    def __init__(self, port="/dev/ttyUSB0", baudrate=115200, onMessage=None):
        """
        onMessage: function(msg: dict) -> None
            보드로부터 한 줄(JSON) 올 때마다 호출됨
        """
        self.port = port
        self.baudrate = baudrate
        self.onMessage = onMessage
        self._ser = None
        self._running = False

    def start(self):
        if serial is None:
            raise RuntimeError("pyserial이 설치되어 있지 않습니다. pip install pyserial")
        self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
        time.sleep(2)  # 보드가 USB 연결 시 자동 리셋되는 시간 대기
        self._running = True
        threading.Thread(target=self._recvLoop, daemon=True).start()
        print(f"[serialModule] connected {self.port} @ {self.baudrate}bps")

    def stop(self):
        self._running = False
        if self._ser:
            self._ser.close()

    def _recvLoop(self):
        while self._running:
            try:
                line = self._ser.readline()
            except OSError:
                break
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if self.onMessage:
                self.onMessage(msg)


if __name__ == "__main__":
    # 단독 실행 시 테스트용 — 픽업보드에서 오는 메시지를 그냥 출력만 함
    def printMessage(msg):
        print("받은 메시지:", msg)

    module = SerialModule(port="/dev/ttyUSB0", onMessage=printMessage)
    module.start()
    print("픽업보드 메시지 대기 중... Ctrl+C로 종료")
    while True:
        time.sleep(1)
