#!/usr/bin/python3
"""검증한 16 B 원본 생성기를 이 실습의 .deps/에 준비한다. sudo 없이 실행한다."""

from pathlib import Path
import os
import subprocess


HERE = Path(__file__).resolve().parent
GENERATOR = HERE / '.deps' / 'TrafficGenerator'
GENERATOR_URL = 'https://github.com/HKUST-SING/TrafficGenerator.git'
GENERATOR_COMMIT = '8e7c51680890fb3d91b7bc3e9a56da8e987f1bf7'
PROFESSOR_COMMIT = '076079bbfc853c4eddd086acacb767e5691a49bf'


def generator_git(*args):
    # main.py는 sudo로 실행된다. 이 경로만 신뢰하고 전역 Git 설정은 변경하지 않는다.
    return subprocess.run(
        ['git', '-c', 'safe.directory=' + str(GENERATOR), '-C', str(GENERATOR), *args],
        check=True, capture_output=True, text=True, timeout=15).stdout.strip()


def verify_generator(require_binaries=True):
    if not (GENERATOR / '.git').is_dir():
        raise ValueError('Run /usr/bin/python3 experiments/professor_capture/prepare_generator.py first')
    if generator_git('rev-parse', 'HEAD') != GENERATOR_COMMIT:
        raise ValueError('Generator commit differs from the supported 16-byte protocol version')
    generator_git('diff', '--exit-code', 'HEAD', '--')
    if require_binaries:
        for name in ('server', 'client'):
            if not os.access(GENERATOR / 'bin' / name, os.X_OK):
                raise ValueError('Generator is not built; run prepare_generator.py first')


def main():
    if os.geteuid() == 0:
        raise SystemExit('Run this preparation as your normal user, without sudo.')
    GENERATOR.parent.mkdir(parents=True, exist_ok=True)
    if not GENERATOR.exists():
        subprocess.run(['git', 'clone', '--no-checkout', GENERATOR_URL, str(GENERATOR)], check=True)
        generator_git('checkout', '--detach', GENERATOR_COMMIT)
    # 기존 파일을 덮어쓰거나 reset하지 않고, 다른 버전/수정 사항이 있으면 중단한다.
    verify_generator(require_binaries=False)
    # upstream Makefile의 move 대상과 경합하지 않도록 병렬 make를 사용하지 않는다.
    subprocess.run(['make', '-C', str(GENERATOR)], check=True)
    verify_generator()
    print('READY: TrafficGenerator ' + GENERATOR_COMMIT)
    print('PATH: ' + str(GENERATOR))


if __name__ == '__main__':
    main()
