"""Генерирует иконки PNG для расширения. Запуск: python make_icons.py"""
import struct, zlib, base64, os

def png(size, r, g, b):
    """Минимальный PNG — одноцветный квадрат с иконкой."""
    # Создаём PNG данные вручную
    def chunk(name, data):
        c = zlib.crc32(name + data) & 0xffffffff
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', c)

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    rows = []
    for y in range(size):
        row = b'\x00'
        for x in range(size):
            # Градиент фиолетовый
            t = (x + y) / (2 * size)
            cr = int(99  + (168 - 99)  * t)
            cg = int(102 + (85  - 102) * t)
            cb = int(241 + (247 - 241) * t)
            row += bytes([cr, cg, cb])
        rows.append(row)
    idat = zlib.compress(b''.join(rows))

    sig = b'\x89PNG\r\n\x1a\n'
    return (sig
            + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', idat)
            + chunk(b'IEND', b''))

os.makedirs('icons', exist_ok=True)
for size in [16, 48, 128]:
    with open(f'icons/icon{size}.png', 'wb') as f:
        f.write(png(size, 99, 102, 241))
    print(f'✓ icon{size}.png')

print('Иконки готовы.')
