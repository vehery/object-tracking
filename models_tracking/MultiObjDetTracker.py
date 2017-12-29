import pickle
import os, cv2
import numpy as np
from models_detection.KerasYOLO import KerasYOLO
from utils.preprocessing import parse_annotation, BatchSequenceGenerator
from utils.utils import WeightReader, decode_netout, draw_boxes, normalize

import tensorflow as tf
import keras.backend as K
K.set_learning_phase(1)

from keras.models import Sequential, Model
from keras.layers import Reshape, Activation, Conv2D, Input, MaxPooling2D, BatchNormalization, Flatten, Dense, Lambda, ConvLSTM2D
from keras.layers.advanced_activations import LeakyReLU
from keras.callbacks import EarlyStopping, ModelCheckpoint, TensorBoard
from keras.optimizers import SGD, Adam, RMSprop
from keras.layers.wrappers import TimeDistributed
from keras.layers.merge import concatenate

IMAGENET_LABEL_MAP = {
                        'n02691156' : 'airplane',
                        'n02419796' : 'antelope',
                        'n02131653' : 'bear',
                        'n02834778' : 'bicycle',
                        'n01503061' : 'bird',
                        'n02924116' : 'bus',
                        'n02958343' : 'car',
                        'n02402425' : 'cattle',
                        'n02084071' : 'dog',
                        'n02121808' : 'domestic cat',
                        'n02503517' : 'elephant',
                        'n02118333' : 'fox',
                        'n02510455' : 'giant panda',
                        'n02342885' : 'hamster',
                        'n02374451' : 'horse',
                        'n02129165' : 'lion',
                        'n01674464' : 'lizard',
                        'n02484322' : 'monkey',
                        'n03790512' : 'motorcycle',
                        'n02324045' : 'rabbit',
                        'n02509815' : 'red panda',
                        'n02411705' : 'sheep',
                        'n01726692' : 'snake',
                        'n02355227' : 'squirrel',
                        'n02129604' : 'tiger',
                        'n04468005' : 'train',
                        'n01662784' : 'turtle',
                        'n04530566' : 'watercraft',
                        'n02062744' : 'whale',
                        'n02391049' : 'zebra'
                    }

class MultiObjDetTracker:

    LABELS_IMAGENET_VIDEO = [
                                'n02691156', 'n02419796', 'n02131653', 'n02834778', 'n01503061', 'n02924116',
                                'n02958343', 'n02402425', 'n02084071', 'n02121808', 'n02503517', 'n02118333',
                                'n02510455', 'n02342885', 'n02374451', 'n02129165', 'n01674464', 'n02484322',
                                'n03790512', 'n02324045', 'n02509815', 'n02411705', 'n01726692', 'n02355227',
                                'n02129604', 'n04468005', 'n01662784', 'n04530566', 'n02062744', 'n02391049'
                            ]

    LABELS           = LABELS_IMAGENET_VIDEO
    IMAGE_H, IMAGE_W = 416, 416
    GRID_H,  GRID_W  = 13 , 13
    BOX              = 5
    CLASS            = len(LABELS)
    CLASS_WEIGHTS    = np.ones(CLASS, dtype='float32')
    OBJ_THRESHOLD    = 0.5 #0.3
    NMS_THRESHOLD    = 0.45 #0.3
    ANCHORS          = [0.57273, 0.677385, 1.87446, 2.06253, 3.33843, 5.47434, 7.88282, 3.52778, 9.77052, 9.16828]

    NO_OBJECT_SCALE  = 1.0
    OBJECT_SCALE     = 5.0
    COORD_SCALE      = 1.0
    CLASS_SCALE      = 1.0

    BATCH_SIZE       = 1
    WARM_UP_BATCHES  = 0
    TRUE_BOX_BUFFER  = 50

    SEQUENCE_LENGTH   = 4
    MAX_BOX_PER_IMAGE = 50

    train_image_folder = 'data/ImageNet-ObjectDetection/ILSVRC2015Train/Data/VID/train/'
    train_annot_folder = 'data/ImageNet-ObjectDetection/ILSVRC2015Train/Annotations/VID/train/'
    valid_image_folder = 'data/ImageNet-ObjectDetection/ILSVRC2015Train/Data/VID/val/'
    valid_annot_folder = 'data/ImageNet-ObjectDetection/ILSVRC2015Train/Annotations/VID/val/'

    model          = None
    detector       = None
    model_detector = None

    def __init__(self, argv=[]):
        self.detector = KerasYOLO()
        if len(argv)!=0:
            self.detector.load_weights(argv[0])
        self.load_model()

    def loss_fxn(self, y_true, y_pred, tboxes, message=''):
        return self.detector.loss_fxn(y_true, y_pred, tboxes, message=message)

    def custom_loss_dtrack(self, y_true, y_pred):
        tboxes    = self.true_boxes
        new_shape = self.BATCH_SIZE * self.SEQUENCE_LENGTH

        y_pred = tf.reshape(y_pred, (new_shape, self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS))
        y_true = tf.reshape(y_true, (new_shape, self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS))
        tboxes = tf.reshape(tboxes, (new_shape, 1, 1, 1, self.TRUE_BOX_BUFFER , 4))

        loss = self.loss_fxn(y_true, y_pred, tboxes=tboxes, message='[DETECTOR] ')
        return loss

    def custom_loss_ttrack(self, y_true, y_pred):
        tboxes    = self.true_boxes
        new_shape = self.BATCH_SIZE * self.SEQUENCE_LENGTH

        y_pred = tf.reshape(y_pred, (new_shape, self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS))
        y_true = tf.reshape(y_true, (new_shape, self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS))
        tboxes = tf.reshape(tboxes, (new_shape, 1, 1, 1, self.TRUE_BOX_BUFFER , 4))

        loss = self.loss_fxn(y_true, y_pred, tboxes=tboxes, message='[TRACKER] ')
        return loss

    def load_model(self):

        self.model_detector = Model( inputs=self.detector.model.input[0],
                                     outputs=[ self.detector.model.get_layer('conv_23').output,
                                               self.detector.model.get_layer('conv_feat').output])
        self.model_detector.summary()

        input_images = Input(batch_shape=(self.BATCH_SIZE, self.SEQUENCE_LENGTH, self.IMAGE_H, self.IMAGE_W, 3), name='images_input')

        outputs, names = [], ['timedist_bbox', 'timedist_vis']
        for i, out in enumerate(self.model_detector.output):
            outputs.append(TimeDistributed(Model(self.model_detector.input, out), name=names[i])(input_images))
        x_bbox, x_vis = outputs

        output_det = TimeDistributed(Reshape((self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS)), name='detection')(x_bbox)

        z = concatenate([x_bbox, x_vis])
        z_vis = ConvLSTM2D(1024, (3,3), strides=(1,1), padding='same', return_sequences=True, name='tconv_lstm')(z)

        # z = TimeDistributed(Conv2D(1024, (3,3), strides=(1,1), padding='same', use_bias=False, name='tconv_1'), name='timedist_tconv1')(z)
        # z = TimeDistributed(BatchNormalization(name='tnorm_1'), name='timedist_tnorm')(z)
        # z_vis = TimeDistributed(LeakyReLU(alpha=0.1))(z)

        z_bbox = TimeDistributed(Conv2D(self.BOX * (4 + 1 + self.CLASS), (1,1), strides=(1,1), padding='same', name='tconv_2'), name='timedist_tconv2')(z_vis)
        z_out = TimeDistributed(Reshape((self.GRID_H, self.GRID_W, self.BOX, 4 + 1 + self.CLASS)))(z_bbox)

        self.true_boxes = Input(batch_shape=(self.BATCH_SIZE, self.SEQUENCE_LENGTH, 1, 1, 1, self.TRUE_BOX_BUFFER , 4), name='bbox_input')
        output_trk = Lambda(lambda args: args[0], name='tracking')([z_out, self.true_boxes])

        self.model = Model([input_images, self.true_boxes], [output_trk, output_det], name='tracker')
        self.model.summary()


    def load_data_generators(self, generator_config):
        train_imgs   = None
        valid_imgs   = None
        train_batch  = None
        valid_batch  = None

        pickle_train = 'data/MultiObjDetTracker_TrainAnn.pickle'
        pickle_val   = 'data/MultiObjDetTracker_ValAnn.pickle'

        if os.path.isfile(pickle_train):
            with open (pickle_train, 'rb') as fp:
               train_imgs = pickle.load(fp)
        else:
            train_imgs, seen_train_labels = parse_annotation(self.train_annot_folder, self.train_image_folder, labels=self.LABELS)
            with open(pickle_train, 'wb') as fp:
               pickle.dump(train_imgs, fp)


        if os.path.isfile(pickle_val):
            with open (pickle_val, 'rb') as fp:
               valid_imgs = pickle.load(fp)
        else:
            valid_imgs, seen_valid_labels = parse_annotation(self.valid_annot_folder, self.valid_image_folder, labels=self.LABELS)
            with open(pickle_val, 'wb') as fp:
               pickle.dump(valid_imgs, fp)


        print "TRAIN GEN", len(train_imgs), generator_config
        train_batch = BatchSequenceGenerator(train_imgs, generator_config, norm=normalize, shuffle=True, jitter=False)
        print "VALID GEN", len(valid_imgs), generator_config
        valid_batch = BatchSequenceGenerator(valid_imgs, generator_config, norm=normalize, jitter=False)

        return train_batch, valid_batch

    def train(self):
        layer   = self.model_detector.layers[-1] # the last convolutional layer
        weights = layer.get_weights()

        new_kernel = np.random.normal(size=weights[0].shape)/(self.GRID_H * self.GRID_W)
        new_bias   = np.random.normal(size=weights[1].shape)/(self.GRID_H * self.GRID_W)

        layer.set_weights([new_kernel, new_bias])

        generator_config = {
            'IMAGE_H'         : self.IMAGE_H,
            'IMAGE_W'         : self.IMAGE_W,
            'GRID_H'          : self.GRID_H,
            'GRID_W'          : self.GRID_W,
            'BOX'             : self.BOX,
            'LABELS'          : self.LABELS,
            'CLASS'           : len(self.LABELS),
            'ANCHORS'         : self.ANCHORS,
            'BATCH_SIZE'      : self.BATCH_SIZE,
            'TRUE_BOX_BUFFER' : 50,
            'SEQUENCE_LENGTH' : self.SEQUENCE_LENGTH
        }

        train_batch, valid_batch = self.load_data_generators(generator_config)
        print "Length of Generators", len(train_batch), len(valid_batch)

        early_stop = EarlyStopping(monitor   = 'val_loss',
                                   min_delta = 0.001,
                                   patience  = 5,
                                   mode      = 'min',
                                   verbose   = 1)

        checkpoint = ModelCheckpoint('weights/WEIGHTS_MultiObjDetTracker.h5',
                                     monitor        = 'val_loss',
                                     verbose        = 1,
                                     save_best_only = True,
                                     # save_weights_only = True,
                                     mode           = 'min',
                                     period         = 1)

        tb_counter  = len([log for log in os.listdir(os.path.expanduser('./logs/')) if 'MultiObjDetTracker_' in log]) + 1
        tensorboard = TensorBoard(log_dir        = os.path.expanduser('./logs/') + 'MultiObjDetTracker_' + str(tb_counter),
                                  histogram_freq = 0,
                                  write_graph    = True,
                                  write_images   = False)

        optimizer = Adam(lr=1e-5, beta_1=0.9, beta_2=0.999, epsilon=1e-08, decay=0.0)
        #optimizer = SGD(lr=1e-4, decay=0.0005, momentum=0.9)
        #optimizer = RMSprop(lr=1e-4, rho=0.9, epsilon=1e-08, decay=0.0)

        self.model.compile(loss=[self.custom_loss_ttrack, self.custom_loss_dtrack], loss_weights=[1.0, 1.0], optimizer=optimizer)
        self.model.fit_generator(
                    generator        = train_batch,
                    steps_per_epoch  = len(train_batch),
                    epochs           = 100,
                    verbose          = 1,
                    validation_data  = valid_batch,
                    validation_steps = len(valid_batch),
                    callbacks        = [early_stop, checkpoint, tensorboard],
                    max_queue_size   = 3)


    def load_weights(self, weight_path):
        self.model.load_weights(weight_path)

    # def predict(self, image_path):
    #     image = cv2.imread(image_path)
    #     resized_image = cv2.resize(image, (self.IMAGE_H, self.IMAGE_W))
    #     resized_image = normalize(resized_image)
    #     input_image = resized_image.reshape((1, self.IMAGE_H, self.IMAGE_W, 3))
    #
    #     dummy_array = np.zeros((1,1,1,1,self.MAX_BOX_PER_IMAGE,4))
    #
    #     netout = self.model.predict([input_image, dummy_array])[0]
    #     boxes  = decode_netout(netout, self.OBJ_THRESHOLD, self.NMS_THRESHOLD, self.ANCHORS, len(self.LABELS))
    #     image = draw_boxes(image, boxes, self.LABELS)
    #
    #     print len(boxes), 'Bounding Boxes Found'
    #     print "File Saved to Output.jpg"
    #     cv2.imwrite('Output.jpg', image)