#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiUdp.h>
 
const char* WIFI_SSID     = "addinedu_201class_2-2.4G";
const char* WIFI_PASSWORD = "201class2!";
const char* SERVER_IP     = "192.168.0.225";  // ★ 실제 서버 PC IP로 재확인 필요(팀 통합 시)
const int   SERVER_PORT   = 6001;  // mainService.py camPorts["dispensing"] 와 일치
 
const int CHUNK_SIZE = 1200;  // 패킷 하나당 최대 데이터 크기 (헤더 제외)
 
// ── 파라미터 (환경에 맞게 하나씩만 바꿔가며 튜닝) ─────────────────
const unsigned long CHUNK_DELAY_MS = 2;    // 청크 사이 delay
const unsigned long FRAME_DELAY_MS = 100;  // 프레임 사이 delay (약 8~9fps 실측 — 아래 참고)
const uint16_t FAIL_STREAK_LIMIT = 30;     // 연속 이만큼 "프레임 전체 송신실패"면 WiFi 재연결 시도
 
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
 
WiFiUDP udp;
uint32_t frameId = 0;
unsigned long lastRssiMs = 0;
uint32_t totalSendFail = 0;
uint16_t consecutiveFullFail = 0;   // 연속으로 "프레임 전체 청크 실패"한 횟수
 
void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결중");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
    if (millis() - start > 15000) {
      Serial.println("\n★ WiFi 15초 넘게 연결 안 됨 — 계속 재시도");
      start = millis();
    }
  }
  Serial.println();
  Serial.print("WiFi 연결됨, IP: ");
  Serial.println(WiFi.localIP());
  Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
 
  WiFi.setSleep(false);   // ★ 절전모드 끄기 — 연속 UDP 송신 중 라디오 슬립으로
                          //   송신 큐가 밀려 endPacket() 이 계속 실패하는 것 방지
}
 
void setup() {
  Serial.begin(115200);
 
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 10000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.jpeg_quality = 12;
 
  bool hasPsram = psramFound();
  Serial.printf("PSRAM 감지: %s / 남은 내부 힙: %u bytes\n",
                hasPsram ? "있음" : "없음", ESP.getFreeHeap());
  // PSRAM 없으면 VGA(640x480) JPEG 프레임버퍼가 내부 RAM에 안 들어가서
  // cam_dma_config 가 malloc 실패로 죽는다. QVGA 도 실패해서 QQVGA 까지 낮춘다.
  config.frame_size = hasPsram ? FRAMESIZE_VGA : FRAMESIZE_QQVGA;
  config.fb_count = hasPsram ? 2 : 1;
 
  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("카메라 초기화 실패! (배선/전원 확인 후 재부팅 필요)");
  } else {
    Serial.println("카메라 초기화 성공");
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
      s->set_vflip(s, 1);      // 1: 상하 반전 적용, 0: 원래대로
      // s->set_hmirror(s, 1); // 좌우도 반전되어 있으면 이 줄 주석 해제
    }
  }
 
  connectWiFi();
  udp.begin(0);
  Serial.printf("전송 대상: %s:%d · CHUNK_DELAY=%lums · FRAME_DELAY=%lums\n",
                SERVER_IP, SERVER_PORT, CHUNK_DELAY_MS, FRAME_DELAY_MS);
 
  lastRssiMs = millis();
}
 
void sendFrameChunked(camera_fb_t* fb) {
  size_t totalLen = fb->len;
  uint16_t totalChunks = (totalLen + CHUNK_SIZE - 1) / CHUNK_SIZE;
  uint16_t sendFailed = 0;
 
  for (uint16_t i = 0; i < totalChunks; i++) {
    size_t offset = i * CHUNK_SIZE;
    size_t len = min((size_t)CHUNK_SIZE, totalLen - offset);
 
    // 헤더: [frame_id(4B)][total_chunks(2B)][chunk_index(2B)]
    uint8_t header[8];
    header[0] = (frameId >> 24) & 0xFF;
    header[1] = (frameId >> 16) & 0xFF;
    header[2] = (frameId >> 8) & 0xFF;
    header[3] = frameId & 0xFF;
    header[4] = (totalChunks >> 8) & 0xFF;
    header[5] = totalChunks & 0xFF;
    header[6] = (i >> 8) & 0xFF;
    header[7] = i & 0xFF;
 
    udp.beginPacket(SERVER_IP, SERVER_PORT);
    udp.write(header, 8);
    udp.write(fb->buf + offset, len);
    if (udp.endPacket() == 0) sendFailed++;   // ★ 이제 실패를 실제로 셈
 
    if (CHUNK_DELAY_MS > 0) delay(CHUNK_DELAY_MS);
  }
 
  totalSendFail += sendFailed;
  // 프레임 전체가 다 실패했는지(=이 프레임은 서버에 아예 안 감) 연속 카운트
  if (totalChunks > 0 && sendFailed == totalChunks) {
    consecutiveFullFail++;
  } else {
    consecutiveFullFail = 0;
  }
 
  Serial.printf("프레임 #%u 전송 (%u bytes, %u개 조각, 송신실패 %u)\n",
                frameId, totalLen, totalChunks, sendFailed);
  frameId++;
 
  // ★ 연속으로 프레임이 통째로 안 나가면(진단 스크립트에서 봤던 "한번 막히면
  //   영구 고착" 패턴) WiFi를 강제로 끊었다 다시 붙여서 복구를 시도한다.
  //   RSSI/힙과 무관하게 일어날 수 있는 드라이버 내부 상태 문제 대응.
  if (consecutiveFullFail >= FAIL_STREAK_LIMIT) {
    Serial.printf("★ 프레임 %u개 연속 전체 송신실패 — WiFi 재연결 시도\n", consecutiveFullFail);
    consecutiveFullFail = 0;
    udp.stop();
    WiFi.disconnect();
    delay(200);
    connectWiFi();
    udp.begin(0);
  }
}
 
void loop() {
  // 5초마다 RSSI/힙/누적 송신실패 — 시리얼 모니터로 무선 상태 감시용
  if (millis() - lastRssiMs > 5000) {
    Serial.printf("  … WiFi RSSI: %d dBm · 남은 힙: %u bytes · 누적 송신실패: %u · WiFi.status()=%d\n",
                  WiFi.RSSI(), ESP.getFreeHeap(), totalSendFail, (int)WiFi.status());
    lastRssiMs = millis();
  }
 
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;
 
  sendFrameChunked(fb);
  esp_camera_fb_return(fb);
 
  if (FRAME_DELAY_MS > 0) delay(FRAME_DELAY_MS);
}