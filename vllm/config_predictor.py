import json
from collections import defaultdict
from typing import Dict, List, Optional

from attr import attrib, attrs


@attrs
class TransformerConfig:
    N = attrib(type=int)
    d_ff = attrib(type=int)
    h = attrib(type=int)
    positional_encoding = attrib(type=dict)
    dropout = attrib(type=float)


@attrs
class FCConfig:
    sizes = attrib(type=List[int])
    input_norm = attrib(type=bool)
    activation = attrib(type=str)
    dropout = attrib(type=float)


@attrs
class PostModelConfig:
    d_output = attrib(type=int)
    output_activation = attrib(type=str)


@attrs
class ModelConfig:
    fc_model = attrib(type=FCConfig)
    transformer = attrib(type=TransformerConfig)
    post_model = attrib(type=PostModelConfig)
    path = attrib(type=str, default="")
    n_features = attrib(type=int, default=4096)
    pred_layer_idx = attrib(type=int, default=31)

@attrs
class PrefillModelConfig:
    pred_model = attrib(type=str) 
    num_labels = attrib(type=int)
    mtype = attrib(type=str)
    activation = attrib(type=str)
    path = attrib(type=str, default="")
    max_length = attrib(type=int, default=1024)
    max_batch_size = attrib(type=int, default=512)

@attrs
class PositionalEncoding:
    strategy = attrib(type=str)
    max_indices = attrib(type=int)


@attrs
class DataConfig:
    path = attrib(type=str)
    num_workers = attrib(type=int)
    batch_size = attrib(type=int)
    slate_length = attrib(type=int)
    validation_ds_role = attrib(type=str)


@attrs
class TrainingConfig:
    epochs = attrib(type=int)
    gradient_clipping_norm = attrib(type=float)
    batch_size = attrib(type=int, default=1)
    early_stopping_patience = attrib(type=int, default=0)



@attrs
class NameArgsConfig:
    name = attrib(type=str)
    args = attrib(type=dict)


@attrs
class PredictorConfig:
    model = attrib(type=ModelConfig)
    #data = attrib(type=DataConfig)
    #optimizer = attrib(type=NameArgsConfig)
    #training = attrib(type=TrainingConfig)
    #loss = attrib(type=NameArgsConfig)
    #metrics = attrib(type=Dict[str, List[int]])
    #lr_scheduler = attrib(type=NameArgsConfig)
    #val_metric = attrib(type=str, default=None)
    #expected_metrics = attrib(type=Dict[str, Dict[str, float]], default={})
    #detect_anomaly = attrib(type=bool, default=False)
    #click_model = attrib(type=Optional[NameArgsConfig], default=None)

    @classmethod
    def from_json(cls, config_path):
        with open(config_path) as config_file:
            config = json.load(config_file)
            return PredictorConfig.from_dict(config)

    @classmethod
    def from_dict(cls, config):
        config["model"] = ModelConfig(**config["model"])
        return cls(**config)

    @staticmethod
    def _parse_metrics(metrics):
        metrics_dict = defaultdict(list)  # type: Dict[str, list]
        for metric_string in metrics:
            try:
                name, at = metric_string.split("_")
                metrics_dict[name].append(int(at))
            except (ValueError, TypeError):
                raise MetricConfigError(
                    metric_string,
                    "Wrong formatting of metric in config. Expected format: <name>_<at> where name is valid metric name and at is and int")
        return metrics_dict

@attrs
class PrefillPredictorConfig:
    model = attrib(type=PrefillModelConfig)
    @classmethod
    def from_json(cls, config_path):
        with open(config_path) as config_file:
            config = json.load(config_file)
            return PrefillPredictorConfig.from_dict(config)

    @classmethod
    def from_dict(cls, config):
        config["model"] = PrefillModelConfig(**config["model"])
        return cls(**config)
        
    @classmethod
    def to_json(cls, config, config_path):
        content = {}
        content["model"] = config.model.__dict__
        print("json content: ", content)
        with open(config_path, "w") as outfile: 
            json.dump(content, outfile)

class MetricConfigError(Exception):
    pass
