import tensorflow as tf

from detection.core.bbox import geometry, transforms

from detection.utils.misc import trim_zeros

class AnchorTarget(object):
    def __init__(self, target_means, target_stds):
        '''Compute regression and classification targets for anchors.
        
        Attributes
        ---
            target_means: [4]. Bounding box refinement mean for RPN.
                Example: (0., 0., 0., 0.)
            target_stds: [4]. Bounding box refinement standard deviation for RPN.
                Example: (0.1, 0.1, 0.2, 0.2)
        '''
        self.target_means = tf.constant(target_means)
        self.target_stds = tf.constant(target_stds)
        
        self.pos_iou_thr = 0.7
        self.neg_iou_thr = 0.3
        
        self.num_rpn_deltas = 256
        self.pos_fraction = 0.5


    def build_targets(self, anchors, valid_flags, gt_boxes, gt_class_ids):
        '''Given the anchors and GT boxes, compute overlaps and identify positive
        anchors and deltas to refine them to match their corresponding GT boxes.

        Args
        ---
            anchors: [num_anchors, (y1, x1, y2, x2)] in image coordinates.
            valid_flags: [batch_size, num_anchors]
            gt_boxes: [batch_size, num_gt_boxes, (y1, x1, y2, x2)] in image 
                coordinates.
            gt_class_ids: [batch_size, num_gt_boxes] Integer class IDs.

        Returns
        ---
            rpn_target_matchs: [batch_size, num_anchors] matches between anchors and GT boxes.
                1 = positive anchor, -1 = negative anchor, 0 = neutral anchor
            rpn_target_deltas: [batch_size, num_rpn_deltas, (dy, dx, log(dh), log(dw))] 
                Anchor bbox deltas.
        '''
        rpn_target_matchs = []
        rpn_target_deltas = []
        
        num_imgs = gt_class_ids.shape[0]
        for i in range(num_imgs):
            target_match, target_delta = self._build_single_target(
                anchors, valid_flags[i], gt_boxes[i], gt_class_ids[i])
            rpn_target_matchs.append(target_match)
            rpn_target_deltas.append(target_delta)
        
        rpn_target_matchs = tf.stack(rpn_target_matchs)
        rpn_target_deltas = tf.stack(rpn_target_deltas)
        
        rpn_target_matchs = tf.stop_gradient(rpn_target_matchs)
        rpn_target_deltas = tf.stop_gradient(rpn_target_deltas)
        
        return rpn_target_matchs, rpn_target_deltas

    def _build_single_target(self, anchors, valid_flags, gt_boxes, gt_class_ids):
        '''Compute targets per instance.
        
        Args
        ---
            anchors: [num_anchors, (y1, x1, y2, x2)]
            valid_flags: [num_anchors]
            gt_class_ids: [num_gt_boxes]
            gt_boxes: [num_gt_boxes, (y1, x1, y2, x2)]
        
        Returns
        ---
            target_matchs: [num_anchors]
            target_deltas: [num_rpn_deltas, (dy, dx, log(dh), log(dw))] 
        '''
        gt_boxes, _ = trim_zeros(gt_boxes)
        
        target_matchs = tf.zeros(anchors.shape[0], dtype=tf.int32)
        
        # Compute overlaps [num_anchors, num_gt_boxes]
        overlaps = geometry.compute_overlaps(anchors, gt_boxes)

        # Match anchors to GT Boxes
        # If an anchor overlaps a GT box with IoU >= 0.7 then it's positive.
        # If an anchor overlaps a GT box with IoU < 0.3 then it's negative.
        # Neutral anchors are those that don't match the conditions above,
        # and they don't influence the loss function.
        # However, don't keep any GT box unmatched (rare, but happens). Instead,
        # match it to the closest anchor (even if its max IoU is < 0.3).
        
        neg_values = tf.constant([0, -1])
        pos_values = tf.constant([0, 1])
        
        # 1. Set negative anchors first. They get overwritten below if a GT box is
        # matched to them.
        anchor_iou_argmax = tf.argmax(overlaps, axis=1)
        anchor_iou_max = tf.reduce_max(overlaps, reduction_indices=[1])
        
        target_matchs = tf.where(anchor_iou_max < self.neg_iou_thr, 
                                 -tf.ones(anchors.shape[0], dtype=tf.int32), target_matchs)

        # filter invalid anchors
        target_matchs = tf.where(tf.equal(valid_flags, 1), 
                                 target_matchs, tf.zeros(anchors.shape[0], dtype=tf.int32))

        # 2. Set anchors with high overlap as positive.
        target_matchs = tf.where(anchor_iou_max >= self.pos_iou_thr, 
                                 tf.ones(anchors.shape[0], dtype=tf.int32), target_matchs)

        # 3. Set an anchor for each GT box (regardless of IoU value).        
        gt_iou_argmax = tf.argmax(overlaps, axis=0)
        target_matchs = tf.scatter_update(tf.Variable(target_matchs), gt_iou_argmax, 1)
        
        
        # Subsample to balance positive and negative anchors
        # Don't let positives be more than half the anchors
        ids = tf.where(tf.equal(target_matchs, 1))
        ids = tf.squeeze(ids, 1)
        extra = ids.shape.as_list()[0] - (self.num_rpn_deltas // 2)
        if extra > 0:
            # Reset the extra ones to neutral
            ids = tf.random_shuffle(ids)[:extra]
            target_matchs = tf.scatter_update(target_matchs, ids, 0)
        # Same for negative proposals
        ids = tf.where(tf.equal(target_matchs, -1))
        ids = tf.squeeze(ids, 1)
        extra = ids.shape.as_list()[0] - (self.num_rpn_deltas -
            tf.reduce_sum(tf.cast(tf.equal(target_matchs, 1), tf.int32)))
        if extra > 0:
            # Rest the extra ones to neutral
            ids = tf.random_shuffle(ids)[:extra]
            target_matchs = tf.scatter_update(target_matchs, ids, 0)

        
        # For positive anchors, compute shift and scale needed to transform them
        # to match the corresponding GT boxes.
        ids = tf.where(tf.equal(target_matchs, 1))
        
        a = tf.gather_nd(anchors, ids)
        anchor_idx = tf.gather_nd(anchor_iou_argmax, ids)
        gt = tf.gather(gt_boxes, anchor_idx)
        
        target_deltas = transforms.bbox2delta(
            a, gt, self.target_means, self.target_stds)
        
        padding = tf.maximum(self.num_rpn_deltas - tf.shape(target_deltas)[0], 0)
        target_deltas = tf.pad(target_deltas, [(0, padding), (0, 0)])

        return target_matchs, target_deltas