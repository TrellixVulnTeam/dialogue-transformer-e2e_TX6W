# dialogue-transformer-e2e

This is a repository for End-to-end Dialogue Transformer project for [Statistical Dialogue Systems](http://ufal.mff.cuni.cz/courses/npfl099) course.

## Goals
- [ ] Use PyTorch's `nn.transformer` to implement Sequicity style dialogue system
- [ ] Compare it with existing dialogue systems (probably Sequicity, mainly)
- [ ] Improve performance by utilizing pre-trained LM 

## Papers
Papers related to this work

- [Sequicity](https://www.comp.nus.edu.sg/~kanmy/papers/acl18-sequicity.pdf)
- [Incorporating Copying Mechanism in Sequence-to-Sequence Learning](https://arxiv.org/pdf/1603.06393.pdf) - the copy mechanism referenced from Sequicity, quite an interesting paper
- [Attention Is All You Need](https://arxiv.org/pdf/1706.03762.pdf) - the transformer architecture
- [Hello, It's GPT-2](https://arxiv.org/pdf/1907.05774.pdf)
- [ALBERT: A Lite BERT](https://arxiv.org/pdf/1909.11942.pdf) - IHMO (ondrej) the methods described in this paper might be easier to use with limited computational resources compared to other pretrained transformers (BERT, GPT-2, XLNet, Transformer-XL, ...)
- [Training Tips for the Transfomer Model](https://ufal.mff.cuni.cz/pbml/110/art-popel-bojar.pdf) - A nice paper form UFAL about practical tips for training transformer, might be useful

## Acknowledgements
- The transformer is the official [Tensorflow implementation](https://github.com/tensorflow/models/tree/master/official/transformer).
- Sequicity implementation from the [authors' repository](https://github.com/WING-NUS/sequicity)
