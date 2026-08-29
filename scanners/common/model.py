class Model:

    def __init__(self):

        self.name = ""

        self.display_name = ""

        self.author = ""

        self.source = ""

        self.url = ""

        self.model_key = ""

        self.architecture = ""

        self.model_type = ""

        self.description = ""

        self.base_model = ""

        self.tags = ""

        self.display_tags = []

        self.files = []

        self.media = []

        self.image = ""

        self.preview_count = 0

        self.has_media = False

        self.has_video = False

        self.downloads = 0

        self.likes = 0

        self.created = ""

        self.updated = ""

        self.license = ""

        self.pipeline = ""

        self.parameters = ""

        self.quantization = ""

        self.format = ""

        self.parent_model = ""

        self.gated = False

        self.card_data = {}

        self.sensitive = False

        self.sha = ""

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def as_dict(self):
        return self.__dict__