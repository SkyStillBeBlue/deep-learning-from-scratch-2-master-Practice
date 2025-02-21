import sys

sys.path.append("..")

import cupy as np
import cupyx as cpx
from common.functions import cross_entropy_error as CEE


class SigmoidWithLoss:
    def __init__(self):
        self.params = []
        self.grads = []
        self.y = None
        self.t = None

    def forward(self, x, t):
        self.t = t
        self.y = 1 / (1 + np.exp(-x))

        loss = CEE(np.c_[1 - self.y, self.y], self.t)

        return loss

    def backward(self, dout=1):
        batch_size = self.t.shape[0]
        dx = (self.y - self.t) * dout / batch_size
        return dx


class Embedding:  # embed层其实就是取出权重矩阵中的其中一个词（几个词）
    def __init__(self, W):
        self.params = [W]
        self.grads = [np.zeros_like(W)]
        self.index = None

    def forward(self, index):
        (W,) = self.params  # 浅拷贝
        # 注意这里有解包操作，如果不解包的话，W就是一个列表，而不是一个numpy矩阵。
        self.index = index
        # print(index)
        # print(W[index].shape)
        # import os
        # os.system("pause")
        return W[index]  # index是列表，抽出这个列表里面的数字对应的行

    def backward(self, dout):
        (dW,) = self.grads  # 这个地方直接引用（浅拷贝）了self.grads
        dW[...] = 0
        cpx.scatter_add(dW, self.index, dout)
        return None


class EmbeddingDot:
    # 在上面抽取的基础上，加上了和h的点积
    def __init__(self, W):
        self.embed = Embedding(W)
        # 要先把权重矩阵转化为Embedding对象
        se = self.embed
        self.params = se.params
        self.grads = se.grads
        self.cache = None
        # 用于存入正向传播的中间结果，以便反向传播时使用

    def forward(self, h, index):
        se = self.embed
        targetW = se.forward(index)  # 抽出词向量
        # print(targetW.shape)
        # print(h.shape)
        # import os
        # os.system("pause")  
        out = np.sum(targetW * h, axis=1)
        # 哈夫曼乘，结果是一个矩阵（因为用了mini_batch），然后再对这个矩阵的每一行求和，得到长度为batch_size的向量（一维矩阵）。这个向量就是这一层的输出
        # a[0][1][2][3]... axis=x表示把对应的第x维压缩掉

        self.cache = (h, targetW)
        # 常识：圆括号括起来是元组，方括号括起来是列表

        return out

    def backward(self, dout):
        h, targetW = self.cache
        dout = dout.reshape(dout.shape[0], 1)
        # 变成一个第一维为batch_size，第二维为1的矩阵，才能和h相乘

        dtargetW = dout * h
        # 这里广播了，dout是有batch_size行1列的矩阵，h是一个有batch_sizes行的矩阵，广播后dout的每一行的那个数字都和h的每一个元素相乘。因为正向传播点乘之后相加本身是等价于经过一个加法节点，所以广播就相当于完成了反向传播

        self.embed.backward(dtargetW)  # 这个是Embed层的反向传播

        dh = dout * targetW
        return dh


class UnigramSampler:

    def __init__(self, corpus, power, sample_size):
        self.sample_size = sample_size
        self.word_p, self.vocab_size = self.get_p(corpus, power)

    def get_p(self, corpus, power):
        # 返回值是处理好的概率分布和词汇表大小
        p = {}
        for id in corpus:
            if id not in p:
                p[id] = 0
            p[id] += 1
        p = np.array(list(p.values()))
        p = np.power(p, power)
        p /= np.sum(p)
        return p, len(p)
    
    def get_neg_sample(self, target):
        batch_size = target.shape[0]
        negetive_sample = np.random.choice(self.vocab_size,size=( self.sample_size,batch_size),replace=True,p=self.word_p)
        # GPU上跑以性能为主，就算采样到正采样词，对整体模型影响也不大。如果修改，开销就会变大。
        return negetive_sample

class NegativeSamplingLoss:
    # 在初始化的时候，传入参数权重W，语料库corpus，以及负采样的次数sample_size
    def __init__(self, W, corpus, power=0.75, sample_size=5):
        self.batch_size = None
        self.sample_size = sample_size
        self.sampler = UnigramSampler(corpus, power, sample_size)
        self.embed_dot_layers = [EmbeddingDot(W) for _ in range(sample_size + 1)]
        self.loss_layers = [SigmoidWithLoss() for _ in range(sample_size + 1)]
        self.params, self.grads = [], []
        for layer in self.embed_dot_layers:
            self.params += layer.params
            self.grads += layer.grads

    def pos_forward(self, h, target):
        score = self.embed_dot_layers[0].forward(h, target)
        correct_label = np.ones(self.batch_size, dtype=np.int32)
        loss = self.loss_layers[0].forward(score, correct_label)
        return loss

    def neg_forward(self, h, target):
        negative_label = np.zeros(self.batch_size, dtype=np.int32)
        negative_target = self.sampler.get_neg_sample(target)
        loss=0
        for i in range(self.sample_size):
            score = self.embed_dot_layers[1 + i].forward(h, negative_target[i])
            loss += self.loss_layers[1 + i].forward(score, negative_label)
        return loss

    def forward(self, h, target):
        self.batch_size = target.shape[0]
        loss_1=self.pos_forward(h, target) 
        loss_2=self.neg_forward(h, target)
        return loss_1+loss_2

    def backward(self, dout=1):
        dh = 0
        for i in range(self.sample_size + 1):
            d = self.loss_layers[i].backward(dout)
            dh += self.embed_dot_layers[i].backward(d)
        return dh
