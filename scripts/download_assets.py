"""Download licensed stock illustrations; run from any working directory."""
from pathlib import Path
import sys
import urllib.request

PHOTOS = [
    ('1.jpg', 'photo-1500648767791-00dcc994a43e'),
    ('2.jpg', 'photo-1560250097-0b93528c311a'),
    ('3.jpg', 'photo-1580489944761-15a19d654956'),
    ('4.jpg', 'photo-1438761681033-6461ffad8d80'),
    ('5.jpg', 'photo-1519085360753-af0119f7cbe7'),
    ('6.jpg', 'photo-1494790108377-be9c29b29330'),
    ('hero.jpg', 'photo-1600210492486-724fe5c67fb0'),
]


def main():
    folder = Path(__file__).resolve().parents[1] / 'static' / 'photos'
    folder.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, source in PHOTOS:
        target = folder / name
        if target.exists() and target.stat().st_size > 5000:
            print(f'{name}: already present')
            continue
        width = 1000 if name == 'hero.jpg' else 700
        url = f'https://images.unsplash.com/{source}?fm=jpg&fit=crop&w={width}&q=85'
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
                if response.headers.get_content_type() != 'image/jpeg' or len(data) < 5000:
                    raise ValueError('Unexpected image response')
            temporary = target.with_suffix('.download')
            temporary.write_bytes(data)
            temporary.replace(target)
            print(f'{name}: {len(data)} bytes')
        except Exception as error:
            failed.append(name)
            print(f'{name}: {error}', file=sys.stderr)
    return bool(failed)


if __name__ == '__main__':
    raise SystemExit(main())
