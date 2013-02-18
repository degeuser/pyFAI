# !/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Project: Azimuthal integration
#             https://forge.epn-campus.eu/projects/azimuthal
#
#    File: "$Id$"
#
#    Copyright (C) European Synchrotron Radiation Facility, Grenoble, France
#
#    Principal author:       Jérôme Kieffer (Jerome.Kieffer@ESRF.eu)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

__author__ = "Jérôme Kieffer"
__contact__ = "Jerome.Kieffer@ESRF.eu"
__license__ = "GPLv3+"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "18/02/2013"
__status__ = "development"

import logging, threading
import types, os, sys
import numpy
logger = logging.getLogger("pyFAI.distortion")
from math import ceil, floor
from pyFAI import detectors, ocl_azim_lut
from pyFAI.utils import timeit
class Distortion(object):
    """
    This class applies a distortion correction on an image.

    """
    def __init__(self, detector="detector", shape=None):
        """
        @param detector: detector instance or detector name
        """
        if type(detector) in types.StringTypes:
            self.detector = detectors.detector_factory(detector)
        else:  # we assume it is a Detector instance
            self.detector = detector
        if "max_shape" in dir(self.detector):
            self.shape = self.detector.max_shape
        else:
            self.shape = shape
        self.shape = tuple([int(i) for i in self.shape])
        self._sem = threading.Semaphore()

        self.lut_size = None
        self.pos = None
        self.LUT = None
        self.delta0 = self.delta1 = None  # max size of an pixel on a regular grid ...
        self.integrator = None
    def __repr__(self):
        return os.linesep.join(["Distortion correction for detector:",
                                self.detector.__repr__()])

#    def calc_lut(self,shape):
    def split_pixel(self,):
        pass
    @timeit
    def calc_pos(self):
        pos_corners = numpy.empty((self.shape[0] + 1, self.shape[1] + 1, 2), dtype=numpy.float64)
        d1 = numpy.outer(numpy.arange(self.shape[0] + 1, dtype=numpy.float64), numpy.ones(self.shape[1] + 1, dtype=numpy.float64)) - 0.5
        d2 = numpy.outer(numpy.ones(self.shape[0] + 1, dtype=numpy.float64), numpy.arange(self.shape[1] + 1, dtype=numpy.float64)) - 0.5
        pos_corners[:, :, 0], pos_corners[:, :, 1] = self.detector.calc_cartesian_positions(d1, d2)
        pos_corners[:, :, 0] /= self.detector.pixel1
        pos_corners[:, :, 1] /= self.detector.pixel2
        pos = numpy.empty((self.shape[0], self.shape[1], 4, 2), dtype=numpy.float32)
        pos[:, :, 0, :] = pos_corners[:-1, :-1]
        pos[:, :, 1, :] = pos_corners[:-1, 1: ]
        pos[:, :, 2, :] = pos_corners[1: , 1: ]
        pos[:, :, 3, :] = pos_corners[1: , :-1]
        self.pos = pos
        self.delta0 = int((numpy.ceil(pos_corners[1:, :, 0]) - numpy.floor(pos_corners[:-1, :, 0])).max())
        self.delta1 = int((numpy.ceil(pos_corners[:, 1:, 1]) - numpy.floor(pos_corners[:, :-1, 1])).max())
        return pos

    @timeit
    def calc_LUT_size(self):
        """
        Considering the "half-CCD" spline from ID11 which describes a (1025,2048) detector,
        the physical location of pixels should go from:
        [-17.48634 : 1027.0543, -22.768829 : 2028.3689]
        We chose to discard pixels falling outside the [0:1025,0:2048] range with a lose of intensity
        
        TODO: if done with Cython, avoid working with pos: pos_corners is enough
        
        """
        if self.pos is None:
            pos = self.calc_pos()
        else:
            pos = self.pos
        pos0min = numpy.maximum(numpy.floor(pos[:, :, :, 0].min(axis= -1)).astype(numpy.int32), 0)
        pos1min = numpy.maximum(numpy.floor(pos[:, :, :, 1].min(axis= -1)).astype(numpy.int32), 0)
        pos0max = numpy.minimum(numpy.ceil(pos[:, :, :, 0].max(axis= -1)).astype(numpy.int32) + 1, self.shape[0])
        pos1max = numpy.minimum(numpy.ceil(pos[:, :, :, 1].max(axis= -1)).astype(numpy.int32) + 1, self.shape[1])
        lut_size = numpy.zeros(self.shape, dtype=numpy.int32)
        for i in range(self.shape[0]):
            for j in range(self.shape[1]):
                lut_size[pos0min[i, j]:pos0max[i, j], pos1min[i, j]:pos1max[i, j]] += 1
        self.lut_size = lut_size.max()
        return lut_size

    @timeit
    def calc_LUT(self):
        if self.lut_size is None:
            with self._sem:
                if self.lut_size is None:
                    self.calc_LUT_size()
        lut = numpy.recarray(shape=(self.shape[0] , self.shape[1], self.lut_size), dtype=[("idx", numpy.uint32), ("coef", numpy.float32)])
        outMax = numpy.zeros(self.shape, dtype=numpy.uint32)
        idx = 0
        buffer = numpy.empty((self.delta0, self.delta1))
        print buffer.shape
        quad = Quad(buffer)
        for i in range(self.shape[0]):
            for j in range(self.shape[1]):
                # i,j, idx are indexes of the raw image uncorrected
                quad.reinit(*list(self.pos[i, j, :, :].ravel()))
                # print self.pos[i, j, 0, :], self.pos[i, j, 1, :], self.pos[i, j, 2, :], self.pos[i, j, 3, :]
                try:
                    quad.populate_box()
                except:
                    print "calc_area_vectorial", quad.calc_area_vectorial()
                    print self.pos[i, j, 0, :], self.pos[i, j, 1, :], self.pos[i, j, 2, :], self.pos[i, j, 3, :]
                for ms in range(quad.box_size0):
                    ml = ms + quad.offset0
                    if ml < 0 or ml >= self.shape[0]:
                        continue
                    for ns in range(quad.box_size1):
                        # ms,ns are indexes of the corrected image in short form, ml & nl are the same
                        nl = ns + quad.offset1
                        if nl < 0 or nl >= self.shape[1]:
                            continue
                        k = outMax[ml, nl]
                        lut[ml, nl, k].idx = idx
                        lut[ml, nl, k].coef = quad.box[ms, ns]
                        outMax[ml, nl] = k + 1
                idx += 1
#                return
        lut.shape = self.shape[0] * self.shape[1], self.lut_size
        self.LUT = lut
    @timeit
    def correct(self, image):
        """
        @param image: 2D-array with the image
        """
#        if self.integrator is None:
#            self.integrator = ocl_azim_lut.OCL_LUT_Integrator(self.LUT, self.shape[0] * self.shape[1])
#        out = self.integrator.integrate(image)
        out = numpy.zeros(self.shape)
        lout = out.ravel()
        lin = image.ravel()
        for i in range(self.LUT.shape[0]):
            for j in self.LUT[i]:
                lout[i] += lin[j.idx] * j.coef
        return out

class Quad(object):
    """

                                     |
                                     |
                                     |                       xxxxxA
                                     |      xxxxxxxI'xxxxxxxx     x
                             xxxxxxxxIxxxxxx       |               x
                Bxxxxxxxxxxxx        |             |               x
                x                    |             |               x
                x                    |             |               x
                 x                   |             |                x
                 x                   |             |                x
                 x                   |             |                x
                 x                   |             |                x
                 x                   |             |                x
                  x                  |             |                 x
                  x                  |             |                 x
                  x                  |             |                 x
                  x                 O|             P              A'  x
 -----------------J------------------+--------------------------------L-----------------------
                  x                  |                                 x
                  x                  |                                  x
                  x                  |                                  x
                   x                 |     xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxD
                   CxxxxxxxxxxxxxxxxxKxxxxx
                                     |
                                     |
                                     |
                                     |
        """
    def __init__(self, buffer):
        self.box = buffer
        self.A0 = self.A1 = None
        self.B0 = self.B1 = None
        self.C0 = self.C1 = None
        self.D0 = self.D1 = None
        self.offset0 = self.offset1 = None
        self.box_size0 = self.box_size1 = None
        self.pAB = self.pBC = self.pCD = self.pDA = None
        self.cAB = self.cBC = self.cCD = self.cDA = None
        self.area = None

    def get_idx(self, i, j):
        pass

    def reinit(self, A0, A1, B0, B1, C0, C1, D0, D1):
        self.box[:, :] = 0.0
        self.A0 = A0
        self.A1 = A1
        self.B0 = B0
        self.B1 = B1
        self.C0 = C0
        self.C1 = C1
        self.D0 = D0
        self.D1 = D1
        self.offset0 = int(floor(min(self.A0, self.B0, self.C0, self.D0)))
        self.offset1 = int(floor(min(self.A1, self.B1, self.C1, self.D1)))
        self.box_size0 = int(ceil(max(self.A0, self.B0, self.C0, self.D0))) - self.offset0
        self.box_size1 = int(ceil(max(self.A1, self.B1, self.C1, self.D1))) - self.offset1
        self.A0 -= self.offset0
        self.A1 -= self.offset1
        self.B0 -= self.offset0
        self.B1 -= self.offset1
        self.C0 -= self.offset0
        self.C1 -= self.offset1
        self.D0 -= self.offset0
        self.D1 -= self.offset1
        self.pAB = self.pBC = self.pCD = self.pDA = None
        self.cAB = self.cBC = self.cCD = self.cDA = None
        self.area = None

    def __repr__(self):
        return os.linesep.join(["offset %i,%i size %i, %i" % (self.offset0, self.offset1, self.box_size0, self.box_size1), "box: %s" % self.box[:self.box_size0, :self.box_size1]])

    def init_slope(self):
        if self.pAB is None:
            if self.B0 == self.A0:
                self.pAB = numpy.inf
            else:
                self.pAB = (self.B1 - self.A1) / (self.B0 - self.A0)
            if self.C0 == self.B0:
                self.pBC = numpy.inf
            else:
                 self.pBC = (self.C1 - self.B1) / (self.C0 - self.B0)
            if self.D0 == self.C0:
                self.pCD = numpy.inf
            else:
                self.pCD = (self.D1 - self.C1) / (self.D0 - self.C0)
            if self.A0 == self.D0:
                self.pDA = numpy.inf
            else:
                self.pDA = (self.A1 - self.D1) / (self.A0 - self.D0)
            self.cAB = self.A1 - self.pAB * self.A0
            self.cBC = self.B1 - self.pBC * self.B0
            self.cCD = self.C1 - self.pCD * self.C0
            self.cDA = self.D1 - self.pDA * self.D0


    def calc_area_AB(self, I1, I2):
        if numpy.isfinite(self.pAB):
            return 0.5 * (I2 - I1) * (self.pAB * (I2 + I1) + 2 * self.cAB)
        else:
            return 0
    def calc_area_BC(self, J1, J2):
        if numpy.isfinite(self.pBC):
            return 0.5 * (J2 - J1) * (self.pBC * (J1 + J2) + 2 * self.cBC)
        else:
            return 0
    def calc_area_CD(self, K1, K2):
        if numpy.isfinite(self.pCD):
            return 0.5 * (K2 - K1) * (self.pCD * (K2 + K1) + 2 * self.cCD)
        else:
            return 0
    def calc_area_DA(self, L1, L2):
        if numpy.isfinite(self.pDA):
            return 0.5 * (L2 - L1) * (self.pDA * (L1 + L2) + 2 * self.cDA)
        else:
            return 0
    def calc_area_old(self):
        if self.area is None:
            if self.pAB is None:
                self.init_slope()
            self.area = -self.calc_area_AB(self.A0, self.B0) - \
               self.calc_area_BC(self.B0, self.C0) - \
               self.calc_area_CD(self.C0, self.D0) - \
               self.calc_area_DA(self.D0, self.A0)
        return self.area

    def calc_area_vectorial(self):
        if self.area is None:
            self.area = numpy.cross([self.C0 - self.A0, self.C1 - self.A1], [self.D0 - self.B0, self.D1 - self.B1]) / 2.0
        return self.area
    calc_area = calc_area_vectorial

    def populate_box(self):
        if self.pAB is None:
            self.init_slope()
        self.integrateAB(self.B0, self.A0, self.calc_area_AB)
        self.integrateAB(self.A0, self.D0, self.calc_area_DA)
        self.integrateAB(self.D0, self.C0, self.calc_area_CD)
        self.integrateAB(self.C0, self.B0, self.calc_area_BC)
        if (self.box / self.calc_area()).min() < 0:
            print self.box
            self.box[:, :] = 0
            print "AB"
            self.integrateAB(self.B0, self.A0, self.calc_area_AB)
            print self.box
            self.box[:, :] = 0
            print "DA"
            self.integrateAB(self.A0, self.D0, self.calc_area_DA)
            print self.box
            self.box[:, :] = 0
            print "CD"
            self.integrateAB(self.D0, self.C0, self.calc_area_CD)
            print self.box
            self.box[:, :] = 0
            print "BC"
            self.integrateAB(self.C0, self.B0, self.calc_area_BC)
            print self.box
            print self
            raise RuntimeError()
        self.box /= self.calc_area_vectorial()

    def integrateAB(self, start, stop, calc_area):
        h = 0
#        print start, stop, calc_area(start, stop)
        if start < stop:  # positive contribution
            P = ceil(start)
            dP = P - start
#            print "Integrate", start, P, stop, calc_area(start, stop)
            if P > stop:  # start and stop are in the same unit
                A = calc_area(start, stop)
                if A != 0:
                    AA = abs(A)
                    sign = A / AA
                    dA = (stop - start)  # always positive
#                    print AA, sign, dA
                    h = 0
                    while AA > 0:
                        if dA > AA:
                            dA = AA
                            AA = -1
                        self.box[int(floor(start)), h] += sign * dA
                        AA -= dA
                        h += 1
            else:
                if dP > 0:
                    A = calc_area(start, P)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA
                        h = 0
                        dA = dP
                        while AA > 0:
                            if dA > AA:
                                dA = AA
                                AA = -1
                            self.box[int(floor(P)) - 1, h] += sign * dA
                            AA -= dA
                            h += 1
                # subsection P1->Pn
                for i in range(int(floor(P)), int(floor(stop))):
                    A = calc_area(i, i + 1)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA

                        h = 0
                        dA = 1.0
                        while AA > 0:
                            if dA > AA:
                                dA = AA
                                AA = -1
                            self.box[i , h] += sign * dA
                            AA -= dA
                            h += 1
                # Section Pn->B
                P = floor(stop)
                dP = stop - P
                if dP > 0:
                    A = calc_area(P, stop)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA
                        h = 0
                        dA = abs(dP)
                        while AA > 0:
                            if dA > AA:
                                dA = AA
                                AA = -1
                            self.box[int(floor(P)), h] += sign * dA
                            AA -= dA
                            h += 1
        elif    start > stop:  # negative contribution. Nota is start=stop: no contribution
            P = floor(start)
            if stop > P:  # start and stop are in the same unit
                A = calc_area(start, stop)
                if A != 0:
                    AA = abs(A)
                    sign = A / AA
                    dA = (start - stop)  # always positive
                    h = 0
                    while AA > 0:
                        if dA > AA:
                            dA = AA
                            AA = -1
                        self.box[int(floor(start)), h] += sign * dA
                        AA -= dA
                        h += 1
            else:
                dP = P - start
                if dP < 0:
                    A = calc_area(start, P)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA
                        h = 0
                        dA = abs(dP)
                        while AA > 0:
                            if dA > AA:
                                dA = AA
                                AA = -1
                            self.box[int(floor(P)) , h] += sign * dA
                            AA -= dA
                            h += 1
                # subsection P1->Pn
                for i in range(int(start), int(ceil(stop)), -1):
                    A = calc_area(i, i - 1)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA
                        h = 0
                        dA = 1
                        while AA > 0:
                            if dA > AA:
                                dA = AA
                                AA = -1
                            self.box[i - 1 , h] += sign * dA
                            AA -= dA
                            h += 1
                # Section Pn->B
                P = ceil(stop)
                dP = stop - P
                if dP < 0:
                    A = calc_area(P, stop)
                    if A != 0:
                        AA = abs(A)
                        sign = A / AA
                        h = 0
                        dA = abs(dP)
                        while AA > 0:
                            if dA > AA:
                                dA = AA; AA = -1
                            self.box[int(floor(stop)), h] += sign * dA
                            AA -= dA
                            h += 1
def test():
    import numpy
    buffer = numpy.empty((20, 20))
    Q = Quad(buffer)
    Q.reinit(7.5, 6.5, 2.5, 5.5, 3.5, 1.5, 8.5, 1.5)
    Q.init_slope()
    print Q.calc_area_AB(Q.A0, Q.B0)
    print Q.calc_area_BC(Q.B0, Q.C0)
    print Q.calc_area_CD(Q.C0, Q.D0)
    print Q.calc_area_DA(Q.D0, Q.A0)
    print Q.calc_area()
    Q.populate_box()
    print Q
    print Q.box.sum()

    print("#"*50)

    Q.reinit(8.5, 1.5, 3.5, 1.5, 2.5, 5.5, 7.5, 6.5)
    Q.init_slope()
    print Q.calc_area_AB(Q.A0, Q.B0)
    print Q.calc_area_BC(Q.B0, Q.C0)
    print Q.calc_area_CD(Q.C0, Q.D0)
    print Q.calc_area_DA(Q.D0, Q.A0)
    print Q.calc_area()
    Q.populate_box()
    print Q
    print Q.box.sum()

    Q.reinit(0.9, 0.9, 0.8, 6.9, 4.3, 3.9, 4.3, 0.9)
#    Q = Quad((-0.3, 1.9), (-0.4, 2.9), (1.3, 2.9), (1.3, 1.9))
    Q.init_slope()
    print "calc_area_vectorial", Q.calc_area_vectorial()
    print Q.A0, Q.A1, Q.B0, Q.B1, Q.C0, Q.C1, Q.D0, Q.D1
    print "Slope", Q.pAB, Q.pBC, Q.pCD, Q.pDA
    print Q.calc_area_AB(Q.A0, Q.B0), Q.calc_area_BC(Q.B0, Q.C0), Q.calc_area_CD(Q.C0, Q.D0), Q.calc_area_DA(Q.D0, Q.A0)
    print Q.calc_area()
    Q.populate_box()
    print Q.box.T
    print Q.box.sum()
    print Q.calc_area_vectorial()
#    sys.exit()

    print("#"*50)

    import fabio, numpy
    raw = numpy.arange(256 * 256)
    raw.shape = 256, 256
    det = detectors.FReLoN("frelon_8_8.spline")
    print det, det.max_shape
    dis = Distortion(det)
    print dis
    lut = dis.calc_LUT_size()
    print dis.lut_size
    print lut.mean()

    dis.calc_LUT()
    out = dis.correct(raw)
    fabio.edfimage.edfimage(data=out.astype("float32")).write("test256.edf")
    import pylab
    pylab.imshow(out, interpolation="nearest")
    pylab.show()

if __name__ == "__main__":
    test()
