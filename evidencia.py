"""Create a display copy of a private uploaded photograph in a bounded subprocess."""
import io
import resource
import sys
import warnings

from PIL import Image, ImageOps


def rasterizar(content, max_side):
    Image.MAX_IMAGE_PIXELS = 16_000_000
    warnings.simplefilter("error",Image.DecompressionBombWarning)
    with Image.open(io.BytesIO(content), formats=("JPEG","PNG","WEBP","GIF")) as im:
        if im.width * im.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("La fotografía excede las dimensiones del visor.")
        image = ImageOps.exif_transpose(im)
        image.thumbnail((max_side,max_side))
        output = io.BytesIO()
        image.convert("RGB").save(output,format="JPEG",quality=85)
    return output.getvalue()


if __name__=="__main__":
    resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(5,5))
    sys.stdout.buffer.write(rasterizar(sys.stdin.buffer.read(), int(sys.argv[1])))
