#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "alkana==0.0.3",
#   "fasteners==0.18",
#   "kanalizer==0.1.1",
#   "pydantic==1.10.19",
#   "python-dotenv==1.0.1",
#   "soundcard==0.4.5",
#   "soundfile==0.13.1",
#   "voicevox-core",
# ]
#
# [tool.uv.sources]
# voicevox-core = [
#   { url = "https://github.com/VOICEVOX/voicevox_core/releases/download/0.16.0/voicevox_core-0.16.0-cp310-abi3-manylinux_2_34_x86_64.whl", marker = "platform_machine == 'x86_64' and sys_platform == 'linux'"},
#   { url = "https://github.com/VOICEVOX/voicevox_core/releases/download/0.16.0/voicevox_core-0.16.0-cp310-abi3-manylinux_2_34_aarch64.whl", marker = "platform_machine != 'x86_64' and sys_platform == 'linux'"},
#   { url = "https://github.com/VOICEVOX/voicevox_core/releases/download/0.16.0/voicevox_core-0.16.0-cp310-abi3-win_amd64.whl", marker = "sys_platform != 'linux'"},
# ]
# ///

import argparse
import csv
import io
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import wave
from pathlib import Path

import alkana
import fasteners
import kanalizer
import soundfile as sf
from pydantic import BaseSettings
from voicevox_core import AccelerationMode
from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile

MAIN_DIR = Path(__file__).resolve().parent
APPIMAGE_FILE = os.environ.get('APPIMAGE')
APPIMAGE_DIR = Path(APPIMAGE_FILE).parent if APPIMAGE_FILE else None

URL_REPLACE_TEXT = 'URL'
URL_REGEX = re.compile(r'(https?|ftp)(:\/\/[-_.!~*\'()a-zA-Z0-9;\/?:\@&=+\$,%#]+)')
SPLIT_TEXT_REGEX = re.compile(r'(?<=[\n　。、！？!?」』)）】》])|(?<=\.\s)')

# https://github.com/VOICEVOX/voicevox_vvm/blob/0.16.0/README.md
VVM_TO_STYLE_IDS_MAP = {
    '0.vvm': [0, 1, 2, 3, 4, 5, 6, 7, 8, 10],
    '1.vvm': [14],
    '2.vvm': [15, 16, 17, 18],
    '3.vvm': [9, 61, 62, 63, 64, 65],
    '4.vvm': [11, 21],
    '5.vvm': [19, 22, 36, 37, 38],
    '6.vvm': [29, 30, 31],
    '7.vvm': [27, 28],
    '8.vvm': [23, 24, 25, 26],
    '9.vvm': [12, 32, 33, 34, 35],
    '10.vvm': [39, 40, 41, 42],
    '11.vvm': [43, 44, 45, 47, 48, 49, 50],
    '12.vvm': [51, 52, 53],
    '13.vvm': [54, 55, 56, 57, 58, 59, 60],
    '14.vvm': [67, 68, 69, 70, 71, 72, 73, 74],
    '15.vvm': [13, 20, 46, 66, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86],
    '16.vvm': [87, 88],
    '17.vvm': [89],
    '18.vvm': [90, 91, 92, 93, 94, 95, 96, 97, 98],
    '19.vvm': [99, 100, 101],
    '20.vvm': [102, 103, 104, 105, 106],
    '21.vvm': [107, 108, 109, 110, 111, 112],
}
STYLE_ID_TO_VVM_MAP = {
    _id: vvm for vvm, ids in VVM_TO_STYLE_IDS_MAP.items() for _id in ids
}


def _find_config_dir_path():
    xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
    if xdg_config_home and (Path(xdg_config_home) / 'tts-server').exists():
        return Path(xdg_config_home) / 'tts-server'
    return Path.home() / '.config' / 'tts-server'


CONFIG_DIR = _find_config_dir_path()


def _find_default_path(rel_path):
    if (Path('.').resolve() / rel_path).exists():
        return Path('.').resolve() / rel_path
    elif APPIMAGE_DIR and (APPIMAGE_DIR / rel_path).exists():
        return APPIMAGE_DIR / rel_path
    elif (MAIN_DIR / rel_path).exists():
        return MAIN_DIR / rel_path
    return CONFIG_DIR / rel_path


DEFAULT_ENGLISH_DIC = _find_default_path('english_dic.csv')
DEFAULT_USER_DIC = _find_default_path('user_dic.csv')
if platform.system() == 'Linux':
    DEFAULT_PLAY_COMMAND = 'paplay'
else:
    DEFAULT_PLAY_COMMAND = ''


class Settings(BaseSettings):
    debug: bool = False
    onnxruntime: str = str(MAIN_DIR / 'libvoicevox_onnxruntime.so')
    voicevox_models: str = str(MAIN_DIR / 'vvms')
    open_jtalk_dic: str = str(MAIN_DIR / 'open_jtalk_dic_utf_8-1.11')
    english_dic: str = str(DEFAULT_ENGLISH_DIC)
    user_dic: str = str(DEFAULT_USER_DIC)
    lock_file: str = str(Path(tempfile.gettempdir()) / 'lockfiles/vsay.lock')
    pulse_server: str | None = None
    play_command: str | list[str] = DEFAULT_PLAY_COMMAND
    play_timeout: int | None = 120
    speaker_idx: int | None = None
    batch_num_lines: int = 10
    batch_max_bytes: int = 1024
    r: float = 1.0
    fm: float = 0.0
    english_word_min_length: int = 3
    english_to_kana: bool = True
    use_user_dic: bool = True
    shorten_urls: bool = False
    use_alkana: bool = True
    use_kanalizer: bool = True
    debug_kanalizer: bool = False
    cpu_num_threads: int = 0
    acceleration_mode: AccelerationMode = 'AUTO'
    speaker_id: int = 3

    class Config:
        env_prefix = 'vsay_'
        env_file_encoding = 'utf-8'
        env_file = _find_default_path('.env')
        fields = {
            'debug': {'env': ['vsay_debug', 'debug']},
            'onnxruntime': {'env': ['vsay_onnxruntime', 'onnxruntime']},
            'voicevox_models': {'env': ['vsay_voicevox_models', 'voicevox_models']},
            'open_jtalk_dic': {'env': ['vsay_open_jtalk_dic', 'open_jtalk_dic']},
            'english_dic': {'env': ['vsay_english_dic', 'english_dic']},
            'user_dic': {'env': ['vsay_user_dic', 'user_dic']},
            'lock_file': {'env': ['vsay_lock_file', 'lock_file']},
            'pulse_server': {'env': ['vsay_pulse_server', 'pulse_server']},
            'play_command': {'env': ['vsay_play_command', 'play_command']},
            'speaker_idx': {'env': ['vsay_speaker_idx', 'speaker_idx']},
            'use_alkana': {'env': ['vsay_use_alkana', 'use_alkana']},
            'use_kanalizer': {'env': ['vsay_use_kanalizer', 'use_kanalizer']},
            'debug_kanalizer': {'env': ['jsay_debug_kanalizer', 'debug_kanalizer']},
            'acceleration_mode': {
                'env': ['vsay_acceleration_mode', 'acceleration_mode']
            },
            'speaker_id': {'env': ['vsay_speaker_id', 'speaker_id']},
        }


settings = Settings()
if settings.pulse_server is not None and os.environ.get('PULSE_SERVER') is None:
    os.environ['PULSE_SERVER'] = settings.pulse_server

ENGLISH_DIC = {}
if settings.use_alkana:
    ENGLISH_DIC.update(alkana.data.data)

if Path(settings.english_dic).is_file():
    with open(settings.english_dic, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        tmp_dict = {}
        for r in reader:
            if len(r) == 2 and (key := r[0].lower()) not in tmp_dict:
                tmp_dict[key] = r[1]
        ENGLISH_DIC.update(tmp_dict)
        del reader, tmp_dict

USER_DIC = {}
USER_DATA_REGEX = None
if Path(settings.user_dic).is_file():
    with open(settings.user_dic, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        tmp_dict = {}
        for r in reader:
            if len(r) == 2 and (key := r[0].lower()) not in tmp_dict:
                tmp_dict[key] = r[1]
        USER_DIC.update(tmp_dict)
        USER_DATA_REGEX = re.compile(
            '|'.join(re.escape(k) for k in USER_DIC.keys()), re.IGNORECASE
        )
        del reader, tmp_dict

logger = logging.getLogger(__name__)
for name in [
    'asyncio',
    'tracing.span',
    'voicevox_core.synthesizer',
    'voicevox_core_python_api',
    'ort',
]:
    logging.getLogger(name).setLevel(logging.ERROR)
    del name

if not settings.debug:
    logging.getLogger('voicevox_core').setLevel(logging.WARNING)

__queue: queue.Queue | None = None
__thread: threading.Thread | None = None

__core: Synthesizer | None = None
__onnxruntime: Onnxruntime | None = None
__open_jtalk: OpenJtalk | None = None


def __ensure_core(speaker_id=None, acceleration_mode=settings.acceleration_mode):
    global __core
    global __onnxruntime
    global __open_jtalk

    def is_mode_change_needed():
        if __core is None:
            return False
        if __core.is_gpu_mode and acceleration_mode == AccelerationMode.CPU:
            return True
        if not __core.is_gpu_mode and acceleration_mode == AccelerationMode.GPU:
            return True
        return False

    if __core is None or is_mode_change_needed():
        if __onnxruntime is None:
            __onnxruntime = Onnxruntime.load_once(filename=settings.onnxruntime)
        if __open_jtalk is None:
            __open_jtalk = OpenJtalk(settings.open_jtalk_dic)
        __core = Synthesizer(
            onnxruntime=__onnxruntime,
            open_jtalk=__open_jtalk,
            acceleration_mode=acceleration_mode,
            cpu_num_threads=settings.cpu_num_threads,
        )

    if speaker_id is not None:
        vvm = STYLE_ID_TO_VVM_MAP.get(speaker_id)
        if vvm is None:
            raise ValueError(f'Invalid speaker_id: {speaker_id}')
        with VoiceModelFile.open(Path(settings.voicevox_models) / vvm) as model:
            if not __core.is_loaded_voice_model(model.id):
                __core.load_voice_model(model)


def __ensure_worker():
    global __queue
    global __thread
    if __queue is None:
        __queue = queue.Queue()
    if __thread is None:
        __thread = threading.Thread(target=__worker, args=(__queue,), daemon=True)
    if not __thread.is_alive():
        try:
            __thread.start()
        except RuntimeError:
            __thread = threading.Thread(target=__worker, args=(__queue,), daemon=True)
            __thread.start()


def __worker(q):
    while True:
        try:
            item = q.get()
            logger.debug(item)
            __say(*item)
        except Exception:
            logger.error(traceback.format_exc())


def __say(
    script,
    speed=settings.r,
    fm=settings.fm,
    english_word_min_length=settings.english_word_min_length,
    english_to_kana=settings.english_to_kana,
    use_user_dic=settings.use_user_dic,
    shorten_urls=settings.shorten_urls,
    speaker_id=settings.speaker_id,
    acceleration_mode=settings.acceleration_mode,
):
    audio_bytes = generate_audio_bytes(
        script,
        speed,
        fm,
        english_word_min_length,
        english_to_kana,
        use_user_dic,
        shorten_urls,
        speaker_id,
        acceleration_mode,
    )
    play_sound(audio_bytes)


def say(
    script,
    speed=settings.r,
    fm=settings.fm,
    english_word_min_length=settings.english_word_min_length,
    english_to_kana=settings.english_to_kana,
    use_user_dic=settings.use_user_dic,
    shorten_urls=settings.shorten_urls,
    speaker_id=settings.speaker_id,
    acceleration_mode=settings.acceleration_mode,
    is_threaded=False,
):
    if not isinstance(speed, (float, int)) or speed <= 0:
        raise ValueError('speed must be positive')

    if not isinstance(english_word_min_length, int) or english_word_min_length < 1:
        raise ValueError('english_word_min_length must be positive integer')

    if is_threaded:
        __ensure_worker()
        __queue.put(
            (
                script,
                speed,
                fm,
                english_word_min_length,
                english_to_kana,
                use_user_dic,
                shorten_urls,
                speaker_id,
                acceleration_mode,
            )
        )
    else:
        __say(
            script,
            speed,
            fm,
            english_word_min_length,
            english_to_kana,
            use_user_dic,
            shorten_urls,
            speaker_id,
            acceleration_mode,
        )


def generate_audio_bytes(
    script,
    speed=settings.r,
    fm=settings.fm,
    english_word_min_length=settings.english_word_min_length,
    english_to_kana=settings.english_to_kana,
    use_user_dic=settings.use_user_dic,
    shorten_urls=settings.shorten_urls,
    speaker_id=settings.speaker_id,
    acceleration_mode=settings.acceleration_mode,
):
    global __core
    logger.debug(script)
    all_lines = [l for l in script.splitlines() if len(l.strip()) > 0]
    batch_lines = [
        '\n'.join(all_lines[i : i + settings.batch_num_lines])
        for i in range(0, len(all_lines), settings.batch_num_lines)
    ]

    results = []
    for batch_text in batch_lines:
        logger.debug(batch_text)
        text = remove_bad_characters(batch_text)
        if shorten_urls:
            text = replace_urls(text)
        if use_user_dic:
            text = apply_user_dic(text)
        if english_to_kana:
            text = convert_english_to_kana(text, english_word_min_length)

        texts = split_text_by_max_bytes(text)
        for text in texts:
            if len(text.strip()) == 0:
                continue

            __ensure_core(speaker_id, acceleration_mode)
            audio_query = __core.create_audio_query(text, speaker_id)
            audio_query.speed_scale = speed
            audio_query.pitch_scale = fm
            audio_query.volume_scale = 2.0
            audio_bytes = __core.synthesis(audio_query, speaker_id)
            if len(audio_bytes) > 0:
                results.append(audio_bytes)

            del __core
            __core = None

    return join_audio_bytes_list(results)


def play_sound(
    audio_bytes,
    command=settings.play_command,
    timeout=settings.play_timeout,
    speaker_idx=settings.speaker_idx,
):
    if command:
        play_sound_with_external_command(audio_bytes, command, timeout)
    else:
        play_sound_with_soundcard(audio_bytes, speaker_idx)


@fasteners.interprocess_locked(settings.lock_file)
def play_sound_with_external_command(
    audio_bytes, command=settings.play_command, timeout=settings.play_timeout
):
    p_play = subprocess.Popen(
        command,
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        p_play.communicate(input=audio_bytes, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        p_play.terminate()
        logger.error(e)


@fasteners.interprocess_locked(settings.lock_file)
def play_sound_with_soundcard(audio_bytes, speaker_idx=settings.speaker_idx):
    # lazily importing soundcard because it is slow
    import soundcard as sc

    frames, samplerate = sf.read(io.BytesIO(audio_bytes))
    if speaker_idx is None:
        speaker = sc.default_speaker()
    else:
        speaker = sc.all_speakers()[speaker_idx]

    speaker.play(frames, samplerate)


def remove_bad_characters(text):
    text = text.replace('\n', '　')
    text = text.replace('\0', '')
    logger.debug(text)
    return text


def replace_urls(text):
    result = URL_REGEX.sub(URL_REPLACE_TEXT, text)
    logger.debug(result)
    return result


def apply_user_dic(text):
    if len(USER_DIC) == 0 or USER_DATA_REGEX is None:
        return text

    def replacer(match):
        matched_text = match.group(0)
        return USER_DIC[matched_text.lower()]

    result = USER_DATA_REGEX.sub(replacer, text)
    logger.debug(result)
    return result


def convert_english_to_kana(
    text, english_word_min_length=settings.english_word_min_length
):
    if not isinstance(english_word_min_length, int) or english_word_min_length < 1:
        raise ValueError('english_word_min_length must be positive integer')

    # https://mackro.blog.jp/archives/8479732.html
    output = ''
    while word := re.search(r'[a-zA-Z]{' f'{english_word_min_length}' r',} ?', text):
        converted = word_to_kana(word.group().rstrip(), english_word_min_length)
        if word.group() == f'{converted} ':
            converted += ' '

        output += text[: word.start()] + converted
        text = text[word.end() :]

    result = output + text
    logger.debug(result)
    return result


def word_to_kana(word, english_word_min_length=settings.english_word_min_length):
    if not isinstance(english_word_min_length, int) or english_word_min_length < 1:
        raise ValueError('english_word_min_length must be positive integer')

    if kana := ENGLISH_DIC.get(word.lower()):
        return kana
    else:
        if re.fullmatch(
            # r'(?:[A-Z][a-z]{' f'{english_word_min_length - 1}' r',}){2,}',
            r'(?:[A-Za-z][a-z]+)(?:[A-Z](?:[a-z]+|[A-Z]+))+',
            word,
        ):
            # m = re.match(r'[A-Z][a-z]{' f'{english_word_min_length - 1}' r',}', word)
            m = re.match(r'[A-Za-z][a-z]+', word)
            first = word_to_kana(m.group())
            second = word_to_kana(word[m.end() :])
            return first + second

        if settings.use_kanalizer:
            if re.fullmatch('[A-Z]{3}|w+', word):
                return word
            try:
                kanalizer_result = kanalizer.convert(
                    word.lower(), on_incomplete='error', on_invalid_input='warning'
                )
            except kanalizer.IncompleteConversionError as e:
                if settings.debug_kanalizer:
                    logger.debug('[kanalizer] %s -> %s', word, e)
                return e.incomplete_output

            if settings.debug_kanalizer:
                logger.debug('[kanalizer] %s -> %s', word, kanalizer_result)
            return kanalizer_result

        return word


def join_audio_bytes_list(audio_bytes_list):
    if len(audio_bytes_list) == 1:
        return audio_bytes_list[0]

    if sum(len(b) for b in audio_bytes_list) == 0:
        return b''

    result_bytes = io.BytesIO()
    is_properties_set = False
    with wave.open(result_bytes, 'wb') as fw:
        for audio_bytes in audio_bytes_list:
            if len(audio_bytes) == 0:
                continue
            with wave.open(io.BytesIO(audio_bytes), 'rb') as fr:
                if not is_properties_set:
                    fw.setsampwidth(fr.getsampwidth())
                    fw.setnchannels(fr.getnchannels())
                    fw.setframerate(fr.getframerate())
                    is_properties_set = True
                fw.writeframes(fr.readframes(fr.getnframes()))

    result_bytes.seek(0)
    return result_bytes.read()


def split_text_by_max_bytes(text, max_bytes_len=settings.batch_max_bytes):
    if max_bytes_len <= 0 or len(text.encode()) <= max_bytes_len:
        return [text]

    split_texts = SPLIT_TEXT_REGEX.split(text)
    texts = []
    buf_text = ''
    for split_text in split_texts:
        if len((buf_text + split_text).encode()) <= max_bytes_len:
            buf_text += split_text
        else:
            if len(buf_text) > 0:
                if len(buf_text) > max_bytes_len:
                    logger.warning('batch_max_bytes is too small')
                texts.append(buf_text)
            buf_text = split_text

    if len(buf_text) > 0:
        if len(buf_text) > max_bytes_len:
            logger.warning('batch_max_bytes is too small')
        texts.append(buf_text)

    logger.debug(texts)
    return texts


def _parse_args():
    parser = argparse.ArgumentParser(description='talk with voicevox')
    parser.add_argument('script', nargs='?', default=sys.stdin)
    parser.add_argument('-r', '--speed', type=float, default=settings.r)
    parser.add_argument('-f', '--fm', type=float, default=settings.fm)
    parser.add_argument(
        '-m',
        '--english-word-min-length',
        type=int,
        default=settings.english_word_min_length,
    )
    parser.add_argument('-e', '--english-to-kana', action='store_true')
    parser.add_argument('-d', '--use-user-dic', action='store_true')
    parser.add_argument('-u', '--shorten-urls', action='store_true')
    parser.add_argument('-i', '--speaker-id', type=int, default=settings.speaker_id)
    parser.add_argument('-a', '--acceleration-mode', default=settings.acceleration_mode)
    parser.add_argument('-p', '--print-bytes', action='store_true')
    return parser.parse_args()


def main():
    args = _parse_args()
    log_format = '%(asctime)s %(levelname)s:%(name)s: %(message)s'
    log_format_debug = (
        '%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d: %(message)s'
    )
    if settings.debug:
        logging.basicConfig(level=logging.DEBUG, format=log_format_debug)
    else:
        logging.basicConfig(level=logging.INFO, format=log_format)

    logger.debug(settings.dict())
    logger.debug(args)
    if settings.debug and not settings.play_command:
        # lazily importing soundcard because it is slow
        import soundcard as sc
        logger.debug('speakers: %s', sc.all_speakers())

    if args.script is sys.stdin:
        if args.script.isatty():
            return
        else:
            args.script = ''.join(args.script.readlines())

    if args.print_bytes:
        audio_bytes = generate_audio_bytes(
            args.script,
            args.speed,
            args.fm,
            args.english_word_min_length,
            args.english_to_kana,
            args.use_user_dic,
            args.shorten_urls,
            args.speaker_id,
            args.acceleration_mode,
        )
        sys.stdout.buffer.write(audio_bytes)
        sys.stdout.buffer.flush()
    else:
        say(
            args.script,
            args.speed,
            args.fm,
            args.english_word_min_length,
            args.english_to_kana,
            args.use_user_dic,
            args.shorten_urls,
            args.speaker_id,
            args.acceleration_mode,
        )


if __name__ == '__main__':
    main()
