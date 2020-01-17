# Robust Explanations for Visual Question Answering
This repository contains the code for the following paper:
* B. Patro, S. Patel, V. Namboodiri, _Robust Explanations for Visual Question Answering_ in WACV, 2020([PDF]())

[![Conference](http://img.shields.io/badge/WACV-2020-4b44ce.svg)]()

Structure of the code is borrowed from [here](https://github.com/Seth-Park/MultimodalExplanations)

If you use this code in your research, please consider citing our work.


## Installation and Dataset Download
For installation, please follow the installation process as mentioned [here](https://github.com/Seth-Park/MultimodalExplanations). For dataset download and preprocessing, follow the instructions mentioned for VQA-X.

## Training
1. We use pretrained VQA model which can be downloaded from [here](https://drive.google.com/drive/u/0/folders/1zQ4I8GrALJhvOfdzdgKAMriAHqQjUKal)
2. Modify the `config.py` as per the requirement. Now, we can train the model

```python train.py```
## Generating Explanations
The pretrained model can be downloaded from [here](). Place the pretrained model in the path `model`
Provide the directory as input and run the command:
```cd generate_vqa_exp```   
 ```python generate_explanation.py --ques_file ../VQA-X/Questions/v2_OpenEnded_mscoco_val2014_questions.json --ann_file ../VQA-X/Annotations/v2_mscoco_val2014_annotations.json --exp_file ../VQA-X/Annotations/val_exp_anno.json --gpu 0 --out_dir ../VQA-X/results --folder ../model/ --model_path $PATH_TO_CAFFEMODEL --use_gt --save_att_map```


## References
* [https://github.com/Seth-Park/MultimodalExplanations](https://github.com/Seth-Park/MultimodalExplanations)


