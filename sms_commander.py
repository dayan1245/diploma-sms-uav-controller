import serial
import time
import logging
import re
from pymavlink import mavutil

#  КОНФИГУРАЦИОННЫЕ ПАРАМЕТРЫ 
MODEM_PORT = '/dev/ttyUSB2'      # Порт, к которому подключён GSM/LTE-модем
MODEM_BAUD = 115200              # Скорость обмена с модемом
FC_PORT = '/dev/ttyAMA0'         # Порт для связи с полётным контроллером (UART)
FC_BAUD = 57600                  # Скорость MAVLink-обмена
ALLOWED_PHONE = '+79123456789'   # Номер отправителя, которому разрешено управление
AUTH_CODE = 'CLOVER2026'         # Кодовое слово, обязательное в теле SMS
LOG_FILE = '/var/log/sms_commander.log'

# Настройка системы логирования
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

#  ИНИЦИАЛИЗАЦИЯ МОДЕМА 
def init_modem(modem):
    """
    Настройка GSM/LTE-модема с помощью AT-команд:
    AT, ATE0, AT+CMGF=1, AT+CNMI=2,1,0,0,0, AT+CSQ, AT+CREG?
    """
    # Проверка связи
    modem.write(b'AT\r\n')
    time.sleep(1)
    resp = modem.read(100)
    if b'OK' not in resp:
        raise RuntimeError("Модем не отвечает на AT")
    logging.info("Модем отвечает на AT")

    # Отключение эха
    modem.write(b'ATE0\r\n')
    time.sleep(0.5)
    modem.read(100)

    # Текстовый режим SMS
    modem.write(b'AT+CMGF=1\r\n')
    time.sleep(0.5)
    modem.read(100)

    # Новые SMS сразу направлять на порт
    modem.write(b'AT+CNMI=2,1,0,0,0\r\n')
    time.sleep(0.5)
    modem.read(100)

    # Уровень сигнала (логируется)
    modem.write(b'AT+CSQ\r\n')
    time.sleep(0.5)
    csq = modem.read(100).decode()
    match = re.search(r'\+CSQ:\s*(\d+)', csq)
    if match:
        rssi = int(match.group(1))
        logging.info(f"Уровень сигнала: {rssi} (0-31, >10 норма)")
    else:
        logging.warning("Не удалось получить CSQ")

    # Проверка регистрации в сети
    modem.write(b'AT+CREG?\r\n')
    time.sleep(0.5)
    creg = modem.read(100).decode()
    if '1' in creg or '5' in creg:
        logging.info("Модем зарегистрирован в сети")
    else:
        logging.warning("Модем НЕ зарегистрирован: " + creg)

    logging.info("Инициализация модема завершена")

#  ЧТЕНИЕ SMS 
def read_sms(modem):
    """
    Читает первое входящее SMS из памяти модема.
    Возвращает (номер_отправителя, текст) или (None, None).
    """
    modem.write(b'AT+CMGL="ALL"\r\n')
    time.sleep(1)
    data = modem.read(modem.in_waiting).decode('utf-8', errors='ignore')
    lines = data.split('\r\n')
    for i, line in enumerate(lines):
        if line.startswith('+CMGL:'):
            parts = line.split(',')
            if len(parts) >= 3:
                idx = parts[0].split(':')[1].strip()
                sender = parts[2].strip('"')
                if i+1 < len(lines):
                    text = lines[i+1].strip()
                    # Удаляем сообщение из памяти
                    modem.write(f'AT+CMGD={idx}\r\n'.encode())
                    time.sleep(0.5)
                    return sender, text
    return None, None

#  MAVLINK 
def send_mavlink_command(master, command, param1=0, param2=0):
    """
    Отправка MAVLink-команды COMMAND_LONG.
    command=400 -> MAV_CMD_COMPONENT_ARM_DISARM
    param1=1 -> ARM (взведение), param1=0 -> DISARM (разоружение)
    """
    master.mav.command_long_send(
        master.target_system,
        master.target_component,
        command,
        0,
        param1, param2, 0, 0, 0, 0, 0
    )
    logging.info(f"MAVLink: команда {command}, param1={param1}")

#  ОСНОВНАЯ ФУНКЦИЯ 
def main():
    logging.info("Запуск SMS-коммандера")

    # --- Инициализация модема ---
    try:
        modem = serial.Serial(MODEM_PORT, MODEM_BAUD, timeout=1)
        init_modem(modem)
    except Exception as e:
        logging.error(f"Ошибка модема: {e}")
        return

    # --- Подключение к автопилоту ---
    try:
        master = mavutil.mavlink_connection(FC_PORT, baud=FC_BAUD)
        master.wait_heartbeat(timeout=10)
        logging.info("Соединение с автопилотом установлено")
    except Exception as e:
        logging.error(f"Ошибка автопилота: {e}")
        return

    logging.info("Ожидание SMS-команд (ARM/DISARM/STATUS)...")
    # --- Бесконечный цикл обработки ---
    while True:
        try:
            sender, text = read_sms(modem)
            if sender and text:
                logging.info(f"SMS от {sender}: {text}")

                # Проверка отправителя
                if sender != ALLOWED_PHONE:
                    logging.warning(f"Запрещённый номер: {sender}")
                    continue

                # Проверка формата: "КОМАНДА КОД"
                parts = text.split()
                if len(parts) < 2 or parts[1] != AUTH_CODE:
                    logging.warning(f"Неверный код: {text}")
                    continue

                cmd = parts[0].upper()
                if cmd == 'ARM':
                    send_mavlink_command(master, 400, param1=1.0)
                    logging.info("ARM выполнен (двигатели готовы)")
                elif cmd == 'DISARM':
                    send_mavlink_command(master, 400, param1=0.0)
                    logging.info("DISARM выполнен (двигатели заблокированы)")
                elif cmd == 'STATUS':
                    reply = "OK: автопилот активен, модем в сети"
                    modem.write(f'AT+CMGS="{sender}"\r\n'.encode())
                    time.sleep(1)
                    modem.write((reply + chr(26)).encode())
                    logging.info(f"Ответ STATUS отправлен на {sender}")
                else:
                    logging.warning(f"Неизвестная команда: {cmd}")

            time.sleep(2)   # пауза между опросами

        except KeyboardInterrupt:
            logging.info("Завершение по Ctrl+C")
            break
        except Exception as e:
            logging.error(f"Ошибка в цикле: {e}")
            time.sleep(5)

if __name__ == '__main__':
    main()