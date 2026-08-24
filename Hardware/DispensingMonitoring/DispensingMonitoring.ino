#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiUdp.h>

const char* WIFI_SSID     = "addinedu_201class_2-2.4G";
const char* WIFI_PASSWORD = "201class2!";
const char* SERVER_IP     = "192.168.0.132";
const int   SERVER_PORT   = 6000;

const int CHUNK_SIZE = 1200;  // 패킷 하나당 최대 데이터 크기 (헤더 제외)

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
  config.frame_size = FRAMESIZE_VGA;   // 640x480, 청크 분할이라 화질 다시 올려도 됨
  config.jpeg_quality = 12;
  config.fb_count = psramFound() ? 2 : 1;

  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("카메라 초기화 실패!");
  } else {
    Serial.println("카메라 초기화 성공");
  }

  // 상하반전 보정 (필요시 set_hmirror로 좌우도 뒤집을 수 있음)
  sensor_t* s = esp_camera_sensor_get();
  s->set_vflip(s, 1);      // 1: 상하 반전 적용, 0: 원래대로
  // s->set_hmirror(s, 1); // 좌우도 반전되어 있으면 이 줄 주석 해제

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("WiFi 연결됨, IP: ");
  Serial.println(WiFi.localIP());

  udp.begin(0);
}

void sendFrameChunked(camera_fb_t* fb) {
  size_t totalLen = fb->len;
  uint16_t totalChunks = (totalLen + CHUNK_SIZE - 1) / CHUNK_SIZE;

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
    udp.endPacket();

    delay(2);// 조각 사이 간격은 두지 않음 (필요시 버퍼 폭주 있으면 1ms 정도만 추가)
  }

  Serial.printf("프레임 #%u 전송 (%u bytes, %u개 조각)\n", frameId, totalLen, totalChunks);
  frameId++;
}

void loop() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;

  sendFrameChunked(fb);
  esp_camera_fb_return(fb);

  delay(100);  // 약 15~20fps 목표 (환경에 따라 실측 fps는 달라짐)
}