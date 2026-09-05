from .base import BaseReranker, register_reranker
import logging
import warnings
import numpy as np
@register_reranker("local")
class LocalReranker(BaseReranker):
    encoder = None

    def _get_encoder(self):
        if self.encoder is None:
            from sentence_transformers import SentenceTransformer
            if not self.config.executor.debug:
                from transformers.utils import logging as transformers_logging
                from huggingface_hub.utils import logging as hf_logging
        
                transformers_logging.set_verbosity_error()
                hf_logging.set_verbosity_error()
                logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
                logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.ERROR)
                logging.getLogger("transformers").setLevel(logging.ERROR)
                logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
                logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
                warnings.filterwarnings("ignore", category=FutureWarning)
            self.encoder = SentenceTransformer(self.config.reranker.local.model, trust_remote_code=True)
        return self.encoder

    def _encode_kwargs(self):
        if self.config.reranker.local.encode_kwargs:
            return dict(self.config.reranker.local.encode_kwargs)
        return {}

    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        encoder = self._get_encoder()
        encode_kwargs = self._encode_kwargs()
        s1_feature = encoder.encode(s1,**encode_kwargs,show_progress_bar=True)
        s2_feature = encoder.encode(s2,**encode_kwargs,show_progress_bar=True)
        # s1 is the candidate list; stash its embeddings for diversity-aware selection in Executor.
        self.candidate_embeddings = s1_feature
        sim = encoder.similarity(s1_feature, s2_feature)
        return sim.numpy()

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        encoder = self._get_encoder()
        return encoder.encode(texts, **self._encode_kwargs(), show_progress_bar=False)
