import os
import sys
import numpy as np
import json

import caffe
from caffe import layers as L
from caffe import params as P

from vqa_data_provider_layer import VQADataProvider
from build_val_model import vqa_proto, exp_proto
import config
from tensorboardX import SummaryWriter


def learning_params(param_list):
    param_dicts = []
    for pl in param_list:
        param_dict = {}
        param_dict['lr_mult'] = pl[0]
        if len(pl) > 1:
            param_dict['decay_mult'] = pl[1]
        param_dicts.append(param_dict)
    return param_dicts

fixed_weights = learning_params([[0, 0], [0, 0]])
fixed_weights_lstm = learning_params([[0, 0], [0, 0], [0, 0]])

def pj_x(mode, batchsize, T, exp_T, question_vocab_size, exp_vocab_size):
    n = caffe.NetSpec()
    mode_str = json.dumps({'mode':mode, 'batchsize':batchsize})
    n.data, n.cont, n.img_feature, n.label, n.exp, n.exp_out, n.exp_cont_1, n.exp_cont_2, n.dis_label = L.Python(module='vqa_data_provider_layer', layer='VQADataProviderLayer', param_str=mode_str, ntop=9)

    n.embed_ba = L.Embed(n.data, input_dim=question_vocab_size, num_output=300, weight_filler=dict(type='uniform',min=-0.08,max=0.08), param=fixed_weights)
    n.embed = L.TanH(n.embed_ba) 

    n.exp_embed_ba = L.Embed(n.exp, input_dim=exp_vocab_size, num_output=300, weight_filler=dict(type='uniform', min=-0.08, max=0.08))
    n.exp_embed = L.TanH(n.exp_embed_ba)

    # LSTM1
    n.lstm1 = L.LSTM(\
                   n.embed, n.cont,\
                   recurrent_param=dict(\
                       num_output=1024,\
                       weight_filler=dict(type='uniform',min=-0.08,max=0.08),\
                       bias_filler=dict(type='constant',value=0)),
                   param=fixed_weights_lstm)
    tops1 = L.Slice(n.lstm1, ntop=T, slice_param={'axis':0})
    for i in range(T-1):
        n.__setattr__('slice_first'+str(i), tops1[int(i)])
        n.__setattr__('silence_data_first'+str(i), L.Silence(tops1[int(i)],ntop=0))
    n.lstm1_out = tops1[T-1]
    n.lstm1_reshaped = L.Reshape(n.lstm1_out,reshape_param=dict(shape=dict(dim=[-1,1024])))
    n.lstm1_reshaped_droped = L.Dropout(n.lstm1_reshaped,dropout_param={'dropout_ratio':0.3})
    n.lstm1_droped = L.Dropout(n.lstm1,dropout_param={'dropout_ratio':0.3})
    # LSTM2
    n.lstm2 = L.LSTM(\
                   n.lstm1_droped, n.cont,\
                   recurrent_param=dict(\
                       num_output=1024,\
                       weight_filler=dict(type='uniform',min=-0.08,max=0.08),\
                       bias_filler=dict(type='constant',value=0)),
                   param=fixed_weights_lstm)
    tops2 = L.Slice(n.lstm2, ntop=T, slice_param={'axis':0})
    for i in range(T-1):
        n.__setattr__('slice_second'+str(i), tops2[int(i)])
        n.__setattr__('silence_data_second'+str(i), L.Silence(tops2[int(i)],ntop=0))
    n.lstm2_out = tops2[T-1]
    n.lstm2_reshaped = L.Reshape(n.lstm2_out,reshape_param=dict(shape=dict(dim=[-1,1024])))
    n.lstm2_reshaped_droped = L.Dropout(n.lstm2_reshaped,dropout_param={'dropout_ratio':0.3})
    concat_botom = [n.lstm1_reshaped_droped, n.lstm2_reshaped_droped]
    n.lstm_12 = L.Concat(*concat_botom)


    # Tile question feature
    n.q_emb_resh = L.Reshape(n.lstm_12, reshape_param=dict(shape=dict(dim=[-1,2048,1,1])))
    n.q_emb_tiled_1 = L.Tile(n.q_emb_resh, axis=2, tiles=14)
    n.q_emb_resh_tiled = L.Tile(n.q_emb_tiled_1, axis=3, tiles=14)

    # Embed image feature
    n.i_emb = L.Convolution(n.img_feature, kernel_size=1, stride=1,num_output=2048, pad=0, weight_filler=dict(type='xavier'),param=fixed_weights)

    # Eltwise product and normalization
    n.eltwise = L.Eltwise(n.q_emb_resh_tiled, n.i_emb, eltwise_param={'operation': P.Eltwise.PROD})
    n.eltwise_sqrt = L.SignedSqrt(n.eltwise)
    n.eltwise_l2 = L.L2Normalize(n.eltwise_sqrt)
    n.eltwise_drop = L.Dropout(n.eltwise_l2, dropout_param={'dropout_ratio': 0.3})

    # Attention for VQA
    n.att_conv1 = L.Convolution(n.eltwise_drop, kernel_size=1, stride=1, num_output=512, pad=0, weight_filler=dict(type='xavier'), param=fixed_weights)
    n.att_conv1_relu = L.ReLU(n.att_conv1)
    n.att_conv2 = L.Convolution(n.att_conv1_relu, kernel_size=1, stride=1, num_output=1, pad=0, weight_filler=dict(type='xavier'), param=fixed_weights)
    n.att_reshaped = L.Reshape(n.att_conv2,reshape_param=dict(shape=dict(dim=[-1,1,14*14])))
    n.att_softmax = L.Softmax(n.att_reshaped, axis=2)
    n.att_map = L.Reshape(n.att_softmax,reshape_param=dict(shape=dict(dim=[-1,1,14,14])))
    
    dummy = L.DummyData(shape=dict(dim=[batchsize, 1]), data_filler=dict(type='constant', value=1), ntop=1)
    n.att_feature  = L.SoftAttention(n.img_feature, n.att_map, dummy)
    n.att_feature_resh = L.Reshape(n.att_feature, reshape_param=dict(shape=dict(dim=[-1,2048])))

    # eltwise product + normalization again for VQA
    n.i_emb2 = L.InnerProduct(n.att_feature_resh, num_output=2048, weight_filler=dict(type='xavier'), param=fixed_weights)
    n.eltwise2 = L.Eltwise(n.lstm_12, n.i_emb2, eltwise_param={'operation': P.Eltwise.PROD})
    n.eltwise2_sqrt = L.SignedSqrt(n.eltwise2)
    n.eltwise2_l2 = L.L2Normalize(n.eltwise2_sqrt)
    n.eltwise2_drop = L.Dropout(n.eltwise2_l2, dropout_param={'dropout_ratio': 0.3})

    n.prediction = L.InnerProduct(n.eltwise2_drop, num_output=3000, weight_filler=dict(type='xavier'), param=fixed_weights)
    n.loss = L.SoftmaxWithLoss(n.prediction, n.label)

    # Embed VQA GT answer during training
    n.exp_emb_ans = L.Embed(n.label, input_dim=3000, num_output=300,weight_filler=dict(type='uniform', min=-0.08, max=0.08))
    n.exp_emb_ans_tanh = L.TanH(n.exp_emb_ans)
    n.exp_emb_ans2 = L.InnerProduct(n.exp_emb_ans_tanh, num_output=2048, weight_filler=dict(type='xavier'))

    # Merge VQA answer and visual+textual feature
    n.exp_emb_resh = L.Reshape(n.exp_emb_ans2, reshape_param=dict(shape=dict(dim=[-1,2048,1,1])))
    n.exp_emb_tiled_1 = L.Tile(n.exp_emb_resh, axis=2, tiles=14)
    n.exp_emb_tiled = L.Tile(n.exp_emb_tiled_1, axis=3, tiles=14)
    n.eltwise_emb = L.Convolution(n.eltwise, kernel_size=1, stride=1, num_output=2048, pad=0, weight_filler=dict(type='xavier'))
    n.exp_eltwise = L.Eltwise(n.eltwise_emb,  n.exp_emb_tiled, eltwise_param={'operation': P.Eltwise.PROD})
    n.exp_eltwise_sqrt = L.SignedSqrt(n.exp_eltwise)
    n.exp_eltwise_l2 = L.L2Normalize(n.exp_eltwise_sqrt)
    n.exp_eltwise_drop = L.Dropout(n.exp_eltwise_l2, dropout_param={'dropout_ratio': 0.3})

    # Attention for Explanation
    n.exp_att_conv1 = L.Convolution(n.exp_eltwise_drop, kernel_size=1, stride=1, num_output=512, pad=0, weight_filler=dict(type='xavier'))
    n.exp_att_conv1_relu = L.ReLU(n.exp_att_conv1)
    n.exp_att_conv2 = L.Convolution(n.exp_att_conv1_relu, kernel_size=1, stride=1, num_output=1, pad=0, weight_filler=dict(type='xavier'))
    n.exp_att_reshaped = L.Reshape(n.exp_att_conv2,reshape_param=dict(shape=dict(dim=[-1,1,14*14])))
    n.exp_att_softmax = L.Softmax(n.exp_att_reshaped, axis=2)
    n.exp_att_map = L.Reshape(n.exp_att_softmax,reshape_param=dict(shape=dict(dim=[-1,1,14,14])))
    
    exp_dummy = L.DummyData(shape=dict(dim=[batchsize, 1]), data_filler=dict(type='constant', value=1), ntop=1)
    n.exp_att_feature_prev  = L.SoftAttention(n.img_feature, n.exp_att_map, exp_dummy)
    n.exp_att_feature_resh = L.Reshape(n.exp_att_feature_prev, reshape_param=dict(shape=dict(dim=[-1, 2048])))
    n.exp_att_feature_embed = L.InnerProduct(n.exp_att_feature_resh, num_output=2048, weight_filler=dict(type='xavier'))
    n.exp_lstm12_embed = L.InnerProduct(n.lstm_12, num_output=2048, weight_filler=dict(type='xavier'))
    n.exp_eltwise2 = L.Eltwise(n.exp_lstm12_embed, n.exp_att_feature_embed, eltwise_param={'operation': P.Eltwise.PROD})
    n.exp_att_feature = L.Eltwise(n.exp_emb_ans2, n.exp_eltwise2, eltwise_param={'operation': P.Eltwise.PROD})


    # LSTM1 for Explanation
    n.exp_lstm1 = L.LSTM(\
                   n.exp_embed, n.exp_cont_1,\
                   recurrent_param=dict(\
                       num_output=2048,\
                       weight_filler=dict(type='uniform',min=-0.08,max=0.08),\
                       bias_filler=dict(type='constant',value=0)))

    n.exp_lstm1_dropped = L.Dropout(n.exp_lstm1,dropout_param={'dropout_ratio':0.3})

    # Merge with LSTM1 for explanation
    n.exp_att_resh = L.Reshape(n.exp_att_feature, reshape_param=dict(shape=dict(dim=[1, -1, 2048])))
    n.exp_att_tiled = L.Tile(n.exp_att_resh, axis=0, tiles=exp_T)
    n.exp_eltwise_all = L.Eltwise(n.exp_lstm1_dropped, n.exp_att_tiled, eltwise_param={'operation': P.Eltwise.PROD})
    n.exp_eltwise_all_sqrt = L.SignedSqrt(n.exp_eltwise_all)
    n.exp_eltwise_all_l2 = L.L2Normalize(n.exp_eltwise_all_sqrt)
    n.exp_eltwise_all_drop = L.Dropout(n.exp_eltwise_all_l2, dropout_param={'dropout_ratio': 0.3})

    # LSTM2 for Explanation
    n.exp_lstm2 = L.LSTM(\
                   n.exp_eltwise_all_drop, n.exp_cont_2,\
                   recurrent_param=dict(\
                       num_output=1024,\
                       weight_filler=dict(type='uniform',min=-0.08,max=0.08),\
                       bias_filler=dict(type='constant',value=0)))
    n.exp_lstm2_dropped = L.Dropout(n.exp_lstm2,dropout_param={'dropout_ratio':0.3})
    
    n.exp_prediction = L.InnerProduct(n.exp_lstm2_dropped, num_output=exp_vocab_size, weight_filler=dict(type='xavier'), axis=2)

    n.exp_loss = L.SoftmaxWithLoss(n.exp_prediction, n.exp_out,
                                   loss_param=dict(ignore_label=-1),
                                   softmax_param=dict(axis=2))
    n.exp_accuracy = L.Accuracy(n.exp_prediction, n.exp_out, axis=2, ignore_label=-1)

    return n.to_proto()

###########################################################################################
def dis_net(mode, batchsize, T, exp_T, question_vocab_size, exp_vocab_size):
    n = caffe.NetSpec()
    mode_str = json.dumps({'mode':mode, 'batchsize':batchsize})
    n.data, n.cont, n.img_feature, n.label, n.exp, n.exp_out, n.exp_cont_1, n.exp_cont_2, n.dis_label = L.Python(module='vqa_data_provider_layer', layer='VQADataProviderLayer', param_str=mode_str, ntop=9)
    
    n.vqa_exp_emb = L.Embed(n.exp, input_dim=exp_vocab_size, num_output=exp_vocab_size, weight_filler=dict(type='uniform', min=-0.08, max=0.08))#n.vqa_exp=batchxseqx exp_vocab_size
    n.vqa_exp = L.TanH(n.vqa_exp_emb)
    n.vqa_exp_reshape = L.Reshape(n.vqa_exp, reshape_param=dict(shape=dict(dim=[-1, exp_vocab_size])))
    n.exp_embed_ba=L.InnerProduct(n.vqa_exp_reshape, num_output=300, weight_filler=dict(type='xavier'))
    n.exp_embed_ba_reshape = L.Reshape(n.exp_embed_ba, reshape_param=dict(shape=dict(dim=[-1,batchsize,300])))
    n.exp_embed = L.TanH(n.exp_embed_ba_reshape)

    # Embed VQA GT answer during training
    n.vqa_ans = L.Embed(n.label, input_dim=3000, num_output=3000,weight_filler=dict(type='uniform', min=-0.08, max=0.08))#n.vqa_ans=batchxseqx3000
    n.exp_emb_ans=L.InnerProduct(n.vqa_ans, num_output=300, weight_filler=dict(type='xavier'))
    n.exp_emb_ans_tanh = L.TanH(n.exp_emb_ans)
    n.exp_emb_ans2 = L.InnerProduct(n.exp_emb_ans_tanh, num_output=2048, weight_filler=dict(type='xavier'))

    # Exp LSTM1
    n.exp_lstm1 = L.LSTM(n.exp_embed, n.exp_cont_1,
                   recurrent_param=dict(num_output=1024,weight_filler=dict(type='uniform',min=-0.08,max=0.08),bias_filler=dict(type='constant',value=0)),
                   param=fixed_weights_lstm)
    exp_tops1 = L.Slice(n.exp_lstm1, ntop=exp_T, slice_param={'axis':0})
    for i in range(T-1):
        n.__setattr__('slice_first'+str(i), exp_tops1[int(i)])
        n.__setattr__('silence_data_first'+str(i), L.Silence(exp_tops1[int(i)],ntop=0))
    n.exp_lstm1_out = exp_tops1[T-1]
    n.exp_lstm1_reshaped = L.Reshape(n.exp_lstm1_out,reshape_param=dict(shape=dict(dim=[-1,1024])))
    n.exp_lstm1_reshaped_droped = L.Dropout(n.exp_lstm1_reshaped,dropout_param={'dropout_ratio':0.3})
    n.exp_lstm1_droped = L.Dropout(n.exp_lstm1,dropout_param={'dropout_ratio':0.3})

    # Exp LSTM2
    n.exp_lstm2 = L.LSTM(n.exp_lstm1_droped, n.exp_cont_2,recurrent_param=dict(num_output=1024,weight_filler=dict(type='uniform',min=-0.08,max=0.08),bias_filler=dict(type='constant',value=0)),param=fixed_weights_lstm)
    exp_tops2 = L.Slice(n.exp_lstm2, ntop=exp_T, slice_param={'axis':0})
    for i in range(T-1):
        n.__setattr__('slice_second'+str(i), exp_tops2[int(i)])
        n.__setattr__('silence_data_second'+str(i), L.Silence(exp_tops2[int(i)],ntop=0))
    n.exp_lstm2_out = exp_tops2[T-1]
    n.exp_lstm2_reshaped = L.Reshape(n.exp_lstm2_out,reshape_param=dict(shape=dict(dim=[-1,1024])))
    n.exp_lstm2_reshaped_droped = L.Dropout(n.exp_lstm2_reshaped,dropout_param={'dropout_ratio':0.3})
    concat_botom = [n.exp_lstm1_reshaped_droped, n.exp_lstm2_reshaped_droped]
    n.exp_lstm_12 = L.Concat(*concat_botom)

    #To concat ans and exp 
    concat_ans_exp = [n.exp_emb_ans2, n.exp_lstm_12]
    n.concat_ans_exp_layer= L.Concat(*concat_ans_exp)
    n.concat_ans_exp_layer_dis = L.InnerProduct(n.concat_ans_exp_layer, num_output=1, weight_filler=dict(type='xavier'))
    n.discr_loss = L.SoftmaxWithLoss(n.concat_ans_exp_layer_dis, n.dis_label,loss_param=dict(ignore_label=-1))

    return n.to_proto()


def make_answer_vocab(adic, vocab_size):
    """
    Returns a dictionary that maps words to indices.
    """
    adict = {'':0}
    nadict = {'':1000000}
    vid = 1
    for qid in adic.keys():
        answer_obj = adic[qid]
        answer_list = [ans['answer'] for ans in answer_obj]
        
        for q_ans in answer_list:
            # create dict
            if q_ans in adict:
                nadict[q_ans] += 1
            else:
                nadict[q_ans] = 1
                adict[q_ans] = vid
                vid +=1

    # debug
    nalist = []
    for k,v in sorted(nadict.items(), key=lambda x:x[1]):
        nalist.append((k,v))

    # remove words that appear less than once 
    n_del_ans = 0
    n_valid_ans = 0
    adict_nid = {}
    for i, w in enumerate(nalist[:-vocab_size]):
        del adict[w[0]]
        n_del_ans += w[1]
    for i, w in enumerate(nalist[-vocab_size:]):
        n_valid_ans += w[1]
        adict_nid[w[0]] = i
    
    return adict_nid

def make_question_vocab(qdic):
    """
    Returns a dictionary that maps words to indices.
    """
    vdict = {'':0}
    vid = 1
    for qid in qdic.keys():
        # sequence to list
        q_str = qdic[qid]['qstr']
        q_list = VQADataProvider.seq_to_list(q_str)

        # create dict
        for w in q_list:
            if w not in vdict:
                vdict[w] = vid
                vid +=1

    return vdict

def make_exp_vocab(exp_dic):
    """
    Returns a dictionary that maps words to indices.
    """
    exp_vdict = {'<EOS>': 0}
    exp_vdict[''] = 1
    exp_id = 2
    for qid in exp_dic.keys():
        exp_strs = exp_dic[qid]
        for exp_str in exp_strs:
            exp_list = VQADataProvider.seq_to_list(exp_str)

            for w in exp_list:
                if w not in exp_vdict:
                    exp_vdict[w] = exp_id
                    exp_id += 1

    return exp_vdict


def make_vocab_files():
    """
    Produce the question, answer, and explanation vocabulary files.
    """
    print('making question vocab...', config.QUESTION_VOCAB_SPACE)
    qdic, _, _ = VQADataProvider.load_data(config.QUESTION_VOCAB_SPACE)
    question_vocab = make_question_vocab(qdic)
    print('making answer vocab...', config.ANSWER_VOCAB_SPACE)
    _, adic, _ = VQADataProvider.load_data(config.ANSWER_VOCAB_SPACE)
    answer_vocab = make_answer_vocab(adic, config.NUM_OUTPUT_UNITS)
    print('making explanation vocab...', config.EXP_VOCAB_SPACE)
    _, _, expdic = VQADataProvider.load_data(config.EXP_VOCAB_SPACE)
    explanation_vocab = make_exp_vocab(expdic)
    return question_vocab, answer_vocab, explanation_vocab

def reverse(dict):
    rev_dict = {}
    for k, v in dict.items():
        rev_dict[v] = k
    return rev_dict

def to_str(type, idxs, cont, r_vdict, r_adict, r_exp_vdict):
    if type == 'a':
        return r_adict[idxs]
    elif type == 'q':
        words = []
        for idx in idxs:
                words.append(r_vdict[idx])

        start = 0
        for i, indicator in enumerate(cont):
            if indicator == 1:
                start = i
                break
        start = max(0, start - 1)
        words = words[start:]
    elif type == 'exp':
        words = []
        for idx in idxs:
            if idx == 0:
                break
            words.append(r_exp_vdict[idx])

    return ' '.join(words)

def batch_to_str(type, batch_idx, batch_cont, r_vdict, r_adict, r_exp_vdict):

    converted = []
    for idxs, cont in zip(batch_idx, batch_cont):
        converted.append(to_str(type, idxs, cont, r_vdict, r_adict, r_exp_vdict))
    return converted
def get_one_hot(targets, nb_classes): 
    res = np.eye(nb_classes)[np.array(targets).reshape(-1)] 
    return res.reshape(list(targets.shape)+[nb_classes])
def one_hot(a, num_classes):
  return np.squeeze(np.eye(num_classes)[a.reshape(-1)])

def convertToOneHot(vector, num_classes=None):
    """
    Converts an input 1-D vector of integers into an output
    2-D array of one-hot vectors, where an i'th input value
    of j will set a '1' in the i'th row, j'th column of the
    output array.

    Example:
        v = np.array((1, 0, 4))
        one_hot_v = convertToOneHot(v)
        print one_hot_v

        [[0 1 0 0 0]
         [1 0 0 0 0]
         [0 0 0 0 1]]
    """

    assert isinstance(vector, np.ndarray)
    assert len(vector) > 0

    if num_classes is None:
        num_classes = np.max(vector)+1
    else:
        assert num_classes > 0
        assert num_classes >= np.max(vector)

    result = np.zeros(shape=(len(vector), num_classes))
    vector = vector.astype(int)
    result[np.arange(len(vector)), vector] = 1
    return result.astype(int)
        
def main():
    writer = SummaryWriter('run/exp')
    if not os.path.exists('./model'):
        os.makedirs('./model')

    question_vocab, answer_vocab, exp_vocab = {}, {}, {}

    if os.path.exists('./model/exp_vdict.json'):
        with open('./model/exp_vdict.json','r') as f:
            exp_vocab = json.load(f)
    else:
        question_vocab, answer_vocab, exp_vocab = make_vocab_files()
        with open('./model/exp_vdict.json','w') as f:
            json.dump(exp_vocab, f)

    # since we are using a pretrained network for VQA, we need the exact same vocabulary used for pretraining
    print('restoring vocab')
    with open('./model/vdict.json','r') as f:
        question_vocab = json.load(f)
    with open('./model/adict.json','r') as f:
        answer_vocab = json.load(f)

    r_vdict = reverse(question_vocab)
    r_adict = reverse(answer_vocab)
    r_exp_vdict = reverse(exp_vocab)


    print('question vocab size:', len(question_vocab))
    print('answer vocab size:', len(answer_vocab))
    print('exp vocab size:', len(exp_vocab))

    with open('./model/proto_train.prototxt', 'w') as f:
        f.write(str(pj_x(config.TRAIN_DATA_SPLITS, config.BATCH_SIZE, config.MAX_WORDS_IN_QUESTION, config.MAX_WORDS_IN_EXP, len(question_vocab), len(exp_vocab))))
    
    with open('./model/vqa_proto_test_gt.prototxt', 'w') as f:
        f.write(str(vqa_proto('val', config.VAL_BATCH_SIZE, config.MAX_WORDS_IN_QUESTION, 1, len(question_vocab), len(exp_vocab), use_gt=True)))

    with open('./model/vqa_proto_test_pred.prototxt', 'w') as f:
        f.write(str(vqa_proto('val', config.VAL_BATCH_SIZE, config.MAX_WORDS_IN_QUESTION, 1, len(question_vocab), len(exp_vocab), use_gt=False)))

    with open('./model/exp_proto_test.prototxt', 'w') as f:
        f.write(str(exp_proto('val', config.VAL_BATCH_SIZE, config.MAX_WORDS_IN_QUESTION, 1, len(question_vocab), len(exp_vocab))))
 
    caffe.set_device(config.GPU_ID)
    caffe.set_mode_gpu()
    solver = caffe.get_solver('./pj_x_solver.prototxt')

    caffe.set_device(config.GPU_ID)
    caffe.set_mode_gpu()
    dis_solver = caffe.get_solver('./pj_x_dis_solver.prototxt')

    train_loss = np.zeros(config.MAX_ITERATIONS)
    train_loss_exp = np.zeros(config.MAX_ITERATIONS)
    discr_real_loss = np.zeros(config.MAX_ITERATIONS)
    discr_fake_loss = np.zeros(config.MAX_ITERATIONS)
    discr_fake_for_generator_loss = np.zeros(config.MAX_ITERATIONS)

    train_acc = np.zeros(config.MAX_ITERATIONS)
    results = []

    #This is to  update output
    snapshot_folder = './snapshots/VQA-X/'
    snapshots_iter = 5000
    base_lr = 0.0007
    momentum = 0.9
    momentum2 = 0.999
    weight_decay = 0.000
    lr_w_mult = 1
    lr_b_mult = 2
    epsilon = 1e-8
    momentum_hist = {}
    momentum_hist_solver = {}
    momentum_hist_solver_s = {}
    for layer in dis_solver.net.params:
        m_w = np.zeros_like(dis_solver.net.params[layer][0].data)
        m_b = np.zeros_like(dis_solver.net.params[layer][1].data)
        momentum_hist[layer] = [m_w, m_b]
    
    for layer in solver.net.params:
        m_w = np.zeros_like(solver.net.params[layer][0].data)
        m_b = np.zeros_like(solver.net.params[layer][1].data)
        momentum_hist_solver[layer] = [m_w, m_b]
        momentum_hist_solver_s[layer] = [m_w, m_b]

    train_gen = True
    train_dis = True
    

    for it in range(config.MAX_ITERATIONS):
        solver.net.forward()
        train_loss[it] = solver.net.blobs['loss'].data
        train_loss_exp[it] = solver.net.blobs['exp_loss'].data
        train_acc[it] = solver.net.blobs['exp_accuracy'].data

        #Data for real exp
        real_exp_index=solver.net.blobs['exp_out'].data
        real_exp_index = np.reshape(real_exp_index,(config.BATCH_SIZE*config.MAX_WORDS_IN_EXP))
        real_exp=convertToOneHot(real_exp_index, 8247)
        real_exp = np.reshape(real_exp,(config.MAX_WORDS_IN_EXP, config.BATCH_SIZE, 8247))
        dis_solver.net.blobs['exp_label_one_hot'].data[...]=real_exp
        dis_solver.net.blobs['exp_cont_1'].data[...]=solver.net.blobs['exp_cont_1'].data
        dis_solver.net.blobs['exp_cont_2'].data[...]=solver.net.blobs['exp_cont_2'].data

        #Data for real ans
        real_ans_index=solver.net.blobs['label'].data
        real_ans=convertToOneHot(real_ans_index, 3000)
        dis_solver.net.blobs['ans_label_one_hot'].data[...] = real_ans

        dis_solver.net.blobs['dis_label'].data[...] = np.ones((config.BATCH_SIZE), dtype='float32')
        
        dis_solver.net.forward()
        discr_real_loss[it] = np.copy(dis_solver.net.blobs['discr_loss'].data)
        if train_dis:
            dis_solver.net.backward()

        #Data for fake exp and ans
        fake_ans=solver.net.blobs['prediction'].data
        fake_exp=solver.net.blobs['exp_prediction'].data 
        
        dis_solver.net.blobs['ans_label_one_hot'].data[...] = fake_ans
        dis_solver.net.blobs['exp_label_one_hot'].data[...] = fake_exp
        dis_solver.net.blobs['dis_label'].data[...] = np.zeros((config.BATCH_SIZE), dtype='float32')

        dis_solver.net.forward()
        discr_fake_loss[it] = np.copy(dis_solver.net.blobs['discr_loss'].data)
        if train_dis:
            dis_solver.net.backward()
            for layer in dis_solver.net.params:
                momentum_hist[layer][0] = momentum_hist[layer][0] * momentum + (dis_solver.net.params[layer][0].diff + weight_decay * dis_solver.net.params[layer][0].data) * base_lr * lr_w_mult
                momentum_hist[layer][1] = momentum_hist[layer][1] * momentum + (dis_solver.net.params[layer][1].diff + weight_decay * dis_solver.net.params[layer][1].data) * base_lr * lr_b_mult
                dis_solver.net.params[layer][0].data[...] -= momentum_hist[layer][0]
                dis_solver.net.params[layer][0].data[...] -= momentum_hist[layer][0]
                dis_solver.net.params[layer][1].data[...] -= momentum_hist[layer][1]
                dis_solver.net.params[layer][0].diff[...] *= 0
                dis_solver.net.params[layer][1].diff[...] *= 0

        dis_solver.net.blobs['dis_label'].data[...] = np.ones((config.BATCH_SIZE), dtype='float32')
        dis_solver.net.forward()
        discr_fake_for_generator_loss[it] = np.copy(dis_solver.net.blobs['discr_loss'].data)
        if train_gen:
            dis_solver.net.backward()
            
            solver.net.blobs['prediction'].diff[...] += dis_solver.net.blobs['ans_label_one_hot'].diff
            solver.net.blobs['exp_prediction'].diff[...] += dis_solver.net.blobs['exp_label_one_hot'].diff
            solver.net.backward()

            for layer in solver.net.params:
                momentum_hist_solver[layer][0] = momentum_hist_solver[layer][0] * momentum + (1 - momentum) * solver.net.params[layer][0].diff
                momentum_hist_solver[layer][1] = momentum_hist_solver[layer][1] * momentum + (1 - momentum) * solver.net.params[layer][1].diff
                momentum_hist_solver_s[layer][0]  = momentum_hist_solver_s[layer][0] * momentum2 + (1 - momentum2) * solver.net.params[layer][0].diff ** 2
                momentum_hist_solver_s[layer][1]  = momentum_hist_solver_s[layer][1] * momentum2 + (1 - momentum2) * solver.net.params[layer][1].diff ** 2
                momentum_hist_0_corr = momentum_hist_solver[layer][0] / (1 - momentum ** (it + 1))
                momentum_hist_1_corr = momentum_hist_solver[layer][1] / (1 - momentum ** (it + 1))
                momentum_hist_s_0_corr = momentum_hist_solver_s[layer][0] / (1 - momentum2 ** (it + 1))
                momentum_hist_s_1_corr = momentum_hist_solver_s[layer][1] / (1 - momentum2 ** (it + 1))
                alpha = base_lr / (1 + weight_decay * (it + 1))
                solver.net.params[layer][0].data[...] -= alpha * momentum_hist_0_corr / np.sqrt(momentum_hist_s_0_corr + epsilon)
                solver.net.params[layer][1].data[...] -= alpha * momentum_hist_1_corr / np.sqrt(momentum_hist_s_1_corr + epsilon)
                solver.net.params[layer][0].diff[...] *= 0
                solver.net.params[layer][1].diff[...] *= 0
   
        if it != 0 and it % config.PRINT_INTERVAL == 0:
            print('Iteration:', it)
            c_mean_loss = train_loss[it-config.PRINT_INTERVAL:it].mean()
            c_mean_loss_exp = train_loss_exp[it-config.PRINT_INTERVAL:it].mean()
            c_mean_acc_exp = train_acc[it-config.PRINT_INTERVAL:it].mean()
            dis_mean_loss= discr_real_loss[it-config.PRINT_INTERVAL:it].mean() + discr_fake_loss[it-config.PRINT_INTERVAL:it].mean()
            print('Train loss for vqa:', c_mean_loss)
            print('Train loss for exp:', c_mean_loss_exp)
            print('Train accuracy for exp:', c_mean_acc_exp)
            print('Train discriminator loss:', dis_mean_loss)
            writer.add_scalar("Train loss for VQA", c_mean_loss, it)
            writer.add_scalar("Train loss for EXP", c_mean_loss_exp, it)
            writer.add_scalar("Train accuracy for VQA", c_mean_acc_exp, it)
            writer.add_scalar("Dis mean loss", dis_mean_loss, it)
            

            questions = solver.net.blobs['data'].data.transpose()
            q_cont = solver.net.blobs['cont'].data.transpose()
            answers = solver.net.blobs['label'].data
            generated_exp = solver.net.blobs['exp_prediction'].data
            generated_exp = generated_exp.argmax(axis=2).transpose()
            target_exp = solver.net.blobs['exp_out'].data.transpose()
            exp_out_cont = solver.net.blobs['exp_cont_2'].data.transpose()

            questions_str = batch_to_str('q', questions, q_cont,r_vdict, r_adict, r_exp_vdict)
            answers_str = batch_to_str('a', answers, np.ones_like(answers),r_vdict, r_adict, r_exp_vdict)
            generated_str = batch_to_str('exp', generated_exp, exp_out_cont,r_vdict, r_adict, r_exp_vdict)
            target_str = batch_to_str('exp', target_exp, exp_out_cont,r_vdict, r_adict, r_exp_vdict)

            count = 0
            for ques, ans, exp, target in zip(questions_str, answers_str, generated_str, target_str):
                if count == 10:
                    break
                print('Q:', ques)
                print('A:', ans)
                print('Because...')
                print('\tgenerated:', exp)
                print('\ttarget:', target)
                count += 1
        if it % snapshots_iter == 0:
            path = snapshot_folder + 'generator_model_iter_' + str(it + 10000) + '.caffemodel'
            solver.net.save(path)
        discr_loss_ratio = (discr_real_loss[it] + discr_fake_loss[it]) / discr_fake_for_generator_loss[it]
        if discr_loss_ratio < 1e-1 and train_dis:    
            train_dis = False
            train_gen = True
        if discr_loss_ratio > 5e-1 and not train_dis:
            train_dis = True
            train_gen = True
        if discr_loss_ratio > 1e1 and train_gen:
            train_gen = False
            train_dis = True
    writer.close()

if __name__ == '__main__':
    main()
