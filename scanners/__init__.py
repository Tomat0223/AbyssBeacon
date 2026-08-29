from . import huggingface
from . import modelscope
from . import civitai
from . import civitaired
from . import tensorhub
from . import seaart

ALL_SCANNERS = {

    huggingface.NAME: huggingface,

    modelscope.NAME: modelscope,

    civitai.NAME: civitai,

    civitaired.NAME: civitaired,

    tensorhub.NAME: tensorhub,

    seaart.NAME: seaart,

}