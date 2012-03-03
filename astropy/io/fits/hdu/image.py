# Licensed under a 3-clause BSD style license - see PYFITS.rst

import sys
import numpy as np

from .base import DELAYED, _ValidHDU, ExtensionHDU
from ..header import Header
from ..util import (_is_pseudo_unsigned, _unsigned_zero, _is_int, _pad_length,
                    _normalize_slice, lazyproperty)


class _ImageBaseHDU(_ValidHDU):
    """FITS image HDU base class.

    Attributes
    ----------
    header
        image header

    data
        image data

    _file
        file associated with array

    _datLoc
        starting byte location of data block in file
    """

    # mappings between FITS and numpy typecodes
    # TODO: Maybe make these module-level constants instead...
    NumCode = {8: 'uint8', 16: 'int16', 32: 'int32', 64: 'int64',
               -32: 'float32', -64: 'float64'}
    ImgCode = {'uint8': 8, 'int16': 16, 'uint16': 16, 'int32': 32,
               'uint32': 32, 'int64': 64, 'uint64': 64, 'float32': -32,
               'float64': -64}

    standard_keyword_comments = {
        'SIMPLE': 'conforms to FITS standard',
        'XTENSION': 'Image extension',
        'BITPIX': 'array data type',
        'NAXIS': 'number of array dimensions',
        'GROUPS': 'has groups',
        'PCOUNT': 'number of parameters',
        'GCOUNT': 'number of groups'
    }

    def __init__(self, data=None, header=None, do_not_scale_image_data=False,
                 uint=False, **kwargs):
        from .groups import GroupsHDU

        super(_ImageBaseHDU, self).__init__(data=data, header=header)

        if header is not None:
            if not isinstance(header, Header):
                # TODO: Instead maybe try initializing a new Header object from
                # whatever is passed in as the header--there are various types
                # of objects that could work for this...
                raise ValueError('header must be a Header object')

        if data is DELAYED:
            # Presumably if data is DELAYED then this HDU is coming from an
            # open file, and was not created in memory
            if header is None:
                # this should never happen
                raise ValueError('No header to setup HDU.')

            # if the file is read the first time, no need to copy, and keep it
            # unchanged
            else:
                self._header = header
        else:
            # TODO: Some of this card manipulation should go into the
            # PrimaryHDU and GroupsHDU subclasses
            # construct a list of cards of minimal header
            if isinstance(self, ExtensionHDU):
                c0 = ('XTENSION', 'IMAGE',
                      self.standard_keyword_comments['XTENSION'])
            else:
                c0 = ('SIMPLE', True, self.standard_keyword_comments['SIMPLE'])
            cards = [
                c0,
                ('BITPIX',    8, self.standard_keyword_comments['BITPIX']),
                ('NAXIS',     0, self.standard_keyword_comments['NAXIS'])]

            if isinstance(self, GroupsHDU):
                cards.append(('GROUPS', True,
                             self.standard_keyword_comments['GROUPS']))

            if isinstance(self, (ExtensionHDU, GroupsHDU)):
                cards.append(('PCOUNT',    0,
                              self.standard_keyword_comments['PCOUNT']))
                cards.append(('GCOUNT',    1,
                              self.standard_keyword_comments['GCOUNT']))

            if header is not None:
                hcopy = header.copy(strip=True)
                cards.extend(hcopy.cards)

            self._header = Header(cards)

        self._do_not_scale_image_data = do_not_scale_image_data
        self._uint = uint

        if do_not_scale_image_data:
            self._bzero = 0
            self._bscale = 1
        else:
            self._bzero = self._header.get('BZERO', 0)
            self._bscale = self._header.get('BSCALE', 1)

        # Save off other important values from the header needed to interpret
        # the image data
        self._axes = [self._header.get('NAXIS' + str(axis + 1), 0)
                      for axis in xrange(self._header.get('NAXIS', 0))]
        self._bitpix = self._header.get('BITPIX', 8)
        self._gcount = self._header.get('GCOUNT', 1)
        self._pcount = self._header.get('PCOUNT', 0)
        self._blank = self._header.get('BLANK')

        self._orig_bitpix = self._bitpix
        self._orig_bzero = self._bzero
        self._orig_bscale = self._bscale

        # Set the name attribute if it was provided (if this is an ImageHDU
        # this will result in setting the EXTNAME keyword of the header as
        # well)
        if 'name' in kwargs and kwargs['name']:
            self.name = kwargs['name']

        # Set to True if the data or header is replaced, indicating that
        # update_header should be called
        self._modified = False

        if data is DELAYED:
            return
        else:
            self.data = data
            self.update_header()

    @classmethod
    def match_header(cls, header):
        """
        _ImageBaseHDU is sort of an abstract class for HDUs containing image
        data (as opposed to table data) and should never be used directly.
        """

        raise NotImplementedError

    @property
    def section(self):
        return Section(self)


    @property
    def shape(self):
        """
        Shape of the image array--should be equivalent to ``self.data.shape``.
        """

        # Determine from the values read from the header
        return tuple(reversed(self._axes))

    @property
    def header(self):
        return self._header

    @header.setter
    def header(self, header):
        self._header = header
        self._modified = True
        self.update_header()

    @lazyproperty
    def data(self):
        if len(self._axes) < 1:
            return

        # Numpy array dimensions are in row/column order
        code = _ImageBaseHDU.NumCode[self._orig_bitpix]

        raw_data = self._file.readarray(offset=self._datLoc, dtype=code,
                                        shape=self.shape)
        raw_data.dtype = raw_data.dtype.newbyteorder('>')

        if (self._orig_bzero == 0 and self._orig_bscale == 1 and
            self._blank is None):
            # No further conversion of the data is necessary
            return raw_data

        data = None
        if not (self._orig_bzero == 0 and self._orig_bscale == 1):
            data = self._convert_pseudo_unsigned(raw_data)

        if data is None:
            # In these cases, we end up with floating-point arrays and have to
            # apply bscale and bzero. We may have to handle BLANK and convert
            # to NaN in the resulting floating-point arrays.
            if self._blank is not None:
                blanks = raw_data.flat == self._blank
                # The size of blanks in bytes is the number of elements in
                # raw_data.flat.  However, if we use np.where instead we will
                # only use 8 bytes for each index where the condition is true.
                # So if the number of blank items is fewer than
                # len(raw_data.flat) / 8, using np.where will use less memory
                if blanks.sum() < len(blanks) / 8:
                    blanks = np.where(blanks)

            new_dtype = self._dtype_for_bitpix()
            if new_dtype is not None:
                data = np.array(raw_data, dtype=new_dtype)
            else:  # floating point cases
                if self._file.memmap:
                    data = raw_data.copy()
                # if not memmap, use the space already in memory
                else:
                    data = raw_data

            del raw_data

            if self._orig_bscale != 1:
                np.multiply(data, self._orig_bscale, data)
            if self._orig_bzero != 0:
                data += self._orig_bzero

            if self._blank is not None:
                data.flat[blanks] = np.nan

        self._update_header_scale_info(data.dtype)

        return data

    @data.setter
    def data(self, data):
        self.__dict__['data'] = data
        self._modified = True
        if self.data is not None and not isinstance(data, np.ndarray):
            # Try to coerce the data into a numpy array--this will work, on
            # some level, for most objects
            try:
                data = np.array(data)
            except:
                raise TypeError('data object %r could not be coerced into an '
                                'ndarray' % data)

        if isinstance(data, np.ndarray):
            self._bitpix = _ImageBaseHDU.ImgCode[data.dtype.name]
            self._orig_bitpix = self._bitpix
            self._orig_bscale = 1
            self._orig_bzero = 0
            self._axes = list(data.shape)
            self._axes.reverse()
        elif self.data is None:
            self._axes = []
        else:
            raise ValueError('not a valid data array')

        self.update_header()

    def update_header(self):
        """
        Update the header keywords to agree with the data.
        """

        if not (self._modified or self._header._modified or
                (self._data_loaded and self.shape != self.data.shape)):
            # Not likely that anything needs updating
            return

        old_naxis = self._header.get('NAXIS', 0)

        if 'BITPIX' not in self._header:
            bitpix_comment = self.standard_keyword_comments['BITPIX']
        else:
            bitpix_comment = self._header.comments['BITPIX']

        # Update the BITPIX keyword and ensure it's in the correct
        # location in the header
        self._header.set('BITPIX', self._bitpix, bitpix_comment, after=0)

        # If the data's shape has changed (this may have happened without our
        # noticing either via a direct update to the data.shape attribute) we
        # need to update the internal self._axes
        if self._data_loaded and self.shape != self.data.shape:
            self._axes = list(self.data.shape)
            self._axes.reverse()

        # Update the NAXIS keyword and ensure it's in the correct location in
        # the header
        if 'NAXIS' in self._header:
            naxis_comment = self._header.comments['NAXIS']
        else:
            naxis_comment = self.standard_keyword_comments['NAXIS']
        self._header.set('NAXIS', len(self._axes), naxis_comment,
                         after='BITPIX')

        # TODO: This routine is repeated in several different classes--it
        # should probably be made available as a methond on all standard HDU
        # types
        # add NAXISi if it does not exist
        for idx, axis in enumerate(self._axes):
            naxisn = 'NAXIS' + str(idx + 1)
            if naxisn in self._header:
                self._header[naxisn] = axis
            else:
                if (idx == 0):
                    after = 'NAXIS'
                else:
                    after = 'NAXIS' + str(idx)
                self._header.set(naxisn, axis, after=after)

        # delete extra NAXISi's
        for idx in range(len(self._axes) + 1, old_naxis + 1):
            try:
                del self._header['NAXIS' + str(idx)]
            except KeyError:
                pass

        self._modified = False

    def _update_header_scale_info(self, dtype=None):
        if (not self._do_not_scale_image_data and
            not (self._orig_bzero == 0 and self._orig_bscale == 1)):
            for keyword in ['BSCALE', 'BZERO']:
                try:
                    del self._header[keyword]
                except KeyError:
                    pass

            if dtype is None:
                dtype = self._dtype_for_bitpix()
            if dtype is not None:
                self._header['BITPIX'] = _ImageBaseHDU.ImgCode[dtype.name]

            self._bzero = 0
            self._bscale = 1
            self._bitpix = self._header['BITPIX']

    def scale(self, type=None, option='old', bscale=1, bzero=0):
        """
        Scale image data by using ``BSCALE``/``BZERO``.

        Call to this method will scale `data` and update the keywords
        of ``BSCALE`` and ``BZERO`` in `_header`.  This method should
        only be used right before writing to the output file, as the
        data will be scaled and is therefore not very usable after the
        call.

        Parameters
        ----------
        type : str, optional
            destination data type, use a string representing a numpy
            dtype name, (e.g. ``'uint8'``, ``'int16'``, ``'float32'``
            etc.).  If is `None`, use the current data type.

        option : str
            How to scale the data: if ``"old"``, use the original
            ``BSCALE`` and ``BZERO`` values when the data was
            read/created. If ``"minmax"``, use the minimum and maximum
            of the data to scale.  The option will be overwritten by
            any user specified `bscale`/`bzero` values.

        bscale, bzero : int, optional
            User-specified ``BSCALE`` and ``BZERO`` values.
        """

        if self.data is None:
            return

        # Determine the destination (numpy) data type
        if type is None:
            type = self.NumCode[self._bitpix]
        _type = getattr(np, type)

        # Determine how to scale the data
        # bscale and bzero takes priority
        if (bscale != 1 or bzero != 0):
            _scale = bscale
            _zero = bzero
        else:
            if option == 'old':
                _scale = self._bscale
                _zero = self._bzero
            elif option == 'minmax':
                if issubclass(_type, np.floating):
                    _scale = 1
                    _zero = 0
                else:

                    min = np.minimum.reduce(self.data.flat)
                    max = np.maximum.reduce(self.data.flat)

                    if _type == np.uint8:  # uint8 case
                        _zero = min
                        _scale = (max - min) / (2.0 ** 8 - 1)
                    else:
                        _zero = (max + min) / 2.0

                        # throw away -2^N
                        nbytes = 8 * _type().itemsize
                        _scale = (max - min) / (2.0 ** nbytes - 2)

        # Do the scaling
        if _zero != 0:
            # 0.9.6.3 to avoid out of range error for BZERO = +32768
            self.data += -_zero
            self._header['BZERO'] = _zero
        else:
            try:
                del self._header['BZERO']
            except KeyError:
                pass

        if _scale and _scale != 1:
            self.data /= _scale
            self._header['BSCALE'] = _scale
        else:
            try:
                del self._header['BSCALE']
            except KeyError:
                pass

        if self.data.dtype.type != _type:
            self.data = np.array(np.around(self.data), dtype=_type)

        # Update the BITPIX Card to match the data
        self._bitpix = _ImageBaseHDU.ImgCode[self.data.dtype.name]
        self._bzero = self._header.get('BZERO', 0)
        self._bscale = self._header.get('BSCALE', 1)
        self._header['BITPIX'] = self._bitpix

        # Since the image has been manually scaled, the current
        # bitpix/bzero/bscale now serve as the 'original' scaling of the image,
        # as though the original image has been completely replaced
        self._orig_bitpix = self._bitpix
        self._orig_bzero = self._bzero
        self._orig_bscale = self._bscale

    def _verify(self, option='warn'):
        # update_header can fix some things that would otherwise cause
        # verification to fail, so do that now...
        self.update_header()

        return super(_ImageBaseHDU, self)._verify(option)

    def _writeheader(self, fileobj, checksum=False):
        self.update_header()
        return super(_ImageBaseHDU, self)._writeheader(fileobj, checksum)

    def _writedata_internal(self, fileobj):
        size = 0

        if self.data is not None:
            # Based on the system type, determine the byteorders that
            # would need to be swapped to get to big-endian output
            if sys.byteorder == 'little':
                swap_types = ('<', '=')
            else:
                swap_types = ('<',)
            # deal with unsigned integer 16, 32 and 64 data
            if _is_pseudo_unsigned(self.data.dtype):
                # Convert the unsigned array to signed
                output = np.array(
                    self.data - _unsigned_zero(self.data.dtype),
                    dtype='>i%d' % self.data.dtype.itemsize)
                should_swap = False
            else:
                output = self.data
                byteorder = output.dtype.str[0]
                should_swap = (byteorder in swap_types)

            if not fileobj.simulateonly:
                if should_swap:
                    output.byteswap(True)
                    try:
                        fileobj.writearray(output)
                    finally:
                        output.byteswap(True)
                else:
                    fileobj.writearray(output)

            size += output.size * output.itemsize

        return size

    def _writeto(self, fileobj, checksum=False):
        if not self._data_loaded:
            # Normally this is done when the data is loaded, but since the data
            # is not loaded yet we need to update the header appropriately
            # before writing it
            self._update_header_scale_info()
        return super(_ImageBaseHDU, self)._writeto(fileobj, checksum=checksum)

    def _dtype_for_bitpix(self):
        """
        Determine the dtype that the data should be converted to depending on
        the BITPIX value in the header, and possibly on the BSCALE value as
        well.  Returns None if there should not be any change.
        """

        bitpix = self._orig_bitpix
        # Handle possible conversion to uints if enabled
        if self._uint and self._orig_bscale == 1:
            for bits, dtype in ((16, np.dtype('uint16')),
                                (32, np.dtype('uint32')),
                                (64, np.dtype('uint64'))):
                if bitpix == bits and self._orig_bzero == 1 << (bits - 1):
                    return dtype

        if bitpix > 16:  # scale integers to Float64
            return np.dtype('float64')
        elif bitpix > 0:  # scale integers to Float32
            return np.dtype('float32')

    def _convert_pseudo_unsigned(self, data):
        """
        Handle "pseudo-unsigned" integers, if the user requested it.  Returns
        the converted data array if so; otherwise returns None.

        In this case case, we don't need to handle BLANK to convert it to NAN,
        since we can't do NaNs with integers, anyway, i.e. the user is
        responsible for managing blanks.
        """

        dtype = self._dtype_for_bitpix()
        # bool(dtype) is always False--have to explicitly compare to None; this
        # caused a fair amount of hair loss
        if dtype is not None and dtype.kind == 'u':
            # Convert the input raw data into an unsigned integer array and
            # then scale the data adjusting for the value of BZERO.  Note that
            # we subtract the value of BZERO instead of adding because of the
            # way numpy converts the raw signed array into an unsigned array.
            bits = dtype.itemsize * 8
            data = np.array(data, dtype=dtype)
            data -= np.uint64(1 << (bits - 1))
            return data

    # TODO: Move the GroupsHDU-specific summary code to GroupsHDU itself
    def _summary(self):
        """
        Summarize the HDU: name, dimensions, and formats.
        """

        class_name = self.__class__.__name__

        # if data is touched, use data info.
        if self._data_loaded:
            if self.data is None:
                format = ''
            else:
                format = self.data.dtype.name
                format = format[format.rfind('.')+1:]
        else:
            # if data is not touched yet, use header info.
            format = self.NumCode[self._bitpix]

        # Display shape in FITS-order
        shape = tuple(reversed(self.shape))

        return (self.name, class_name, len(self._header), shape, format, '')

    def _calculate_datasum(self, blocking):
        """
        Calculate the value for the ``DATASUM`` card in the HDU.
        """

        if self._data_loaded and self.data is not None:
            # We have the data to be used.
            d = self.data

            # First handle the special case where the data is unsigned integer
            # 16, 32 or 64
            if _is_pseudo_unsigned(self.data.dtype):
                d = np.array(self.data - _unsigned_zero(self.data.dtype),
                             dtype='i%d' % self.data.dtype.itemsize)

            # Check the byte order of the data.  If it is little endian we
            # must swap it before calculating the datasum.
            if d.dtype.str[0] != '>':
                byteswapped = True
                d = d.byteswap(True)
                d.dtype = d.dtype.newbyteorder('>')
            else:
                byteswapped = False

            cs = self._compute_checksum(np.fromstring(d, dtype='ubyte'),
                                        blocking=blocking)

            # If the data was byteswapped in this method then return it to
            # its original little-endian order.
            if byteswapped and not _is_pseudo_unsigned(self.data.dtype):
                d.byteswap(True)
                d.dtype = d.dtype.newbyteorder('<')

            return cs
        else:
            # This is the case where the data has not been read from the file
            # yet.  We can handle that in a generic manner so we do it in the
            # base class.  The other possibility is that there is no data at
            # all.  This can also be handled in a gereric manner.
            return super(_ImageBaseHDU, self)._calculate_datasum(
                    blocking=blocking)


class Section(object):
    """
    Image section.

    TODO: elaborate
    """

    def __init__(self, hdu):
        self.hdu = hdu

    def __getitem__(self, key):
        dims = []
        if not isinstance(key, tuple):
            key = (key,)
        naxis = len(self.hdu.shape)
        if naxis < len(key):
            raise IndexError('too many indices')
        elif naxis > len(key):
            key = key + (slice(None),) * (naxis - len(key))

        offset = 0

        # Declare outside of loop scope for use below--don't abuse for loop
        # scope leak defect
        idx = 0
        for idx in range(naxis):
            axis = self.hdu.shape[idx]
            indx = _iswholeline(key[idx], axis)
            offset = offset * axis + indx.offset

            # all elements after the first WholeLine must be WholeLine or
            # OnePointAxis
            if isinstance(indx, (_WholeLine, _LineSlice)):
                dims.append(indx.npts)
                break
            elif isinstance(indx, _SteppedSlice):
                raise IndexError('Stepped Slice not supported')

        contiguousSubsection = True

        for jdx in range(idx + 1, naxis):
            axis = self.hdu.shape[jdx]
            indx = _iswholeline(key[jdx], axis)
            dims.append(indx.npts)
            if not isinstance(indx, _WholeLine):
                contiguousSubsection = False

            # the offset needs to multiply the length of all remaining axes
            else:
                offset *= axis

        if contiguousSubsection:
            if not dims:
                dims = [1]

            dims = tuple(dims)

            bitpix = self.hdu._bitpix
            offset = self.hdu._datLoc + (offset * abs(bitpix) // 8)
            # If the data is already loaded use whatever dtype it was scaled
            # to, else figure it out from the bitpix...
            if self.hdu._data_loaded:
                code = self.hdu.data.dtype.name
            else:
                code = _ImageBaseHDU.NumCode[bitpix]
            raw_data = self.hdu._file.readarray(offset=offset, dtype=code,
                                                shape=dims)
            raw_data.dtype = raw_data.dtype.newbyteorder('>')
            data = raw_data
        else:
            data = self._getdata(key)

        if not (self.hdu._bzero == 0 and self.hdu._bscale == 1):
            converted_data = self.hdu._convert_pseudo_unsigned(data)
            if converted_data is not None:
                data = converted_data

        return data

    def _getdata(self, keys):
        out = []
        naxis = len(self.hdu.shape)

        # Determine the number of slices in the set of input keys.
        # If there is only one slice then the result is a one dimensional
        # array, otherwise the result will be a multidimensional array.
        numSlices = 0
        for idx, key in enumerate(keys):
            if isinstance(key, slice):
                numSlices = numSlices + 1

        for idx, key in enumerate(keys):
            if isinstance(key, slice):
                # OK, this element is a slice so see if we can get the data for
                # each element of the slice.
                axis = self.hdu.shape[idx]
                ns = _normalize_slice(key, axis)

                for k in range(ns.start, ns.stop):
                    key1 = list(keys)
                    key1[idx] = k
                    key1 = tuple(key1)

                    if numSlices > 1:
                        # This is not the only slice in the list of keys so
                        # we simply get the data for this section and append
                        # it to the list that is output.  The out variable will
                        # be a list of arrays.  When we are done we will pack
                        # the list into a single multidimensional array.
                        out.append(self[key1])
                    else:
                        # This is the only slice in the list of keys so if this
                        # is the first element of the slice just set the output
                        # to the array that is the data for the first slice.
                        # If this is not the first element of the slice then
                        # append the output for this slice element to the array
                        # that is to be output.  The out variable is a single
                        # dimensional array.
                        if k == ns.start:
                            out = self[key1]
                        else:
                            out = np.append(out, self[key1])

                # We have the data so break out of the loop.
                break

        if isinstance(out, list):
            out = np.array(out)

        return out


class PrimaryHDU(_ImageBaseHDU):
    """
    FITS primary HDU class.
    """

    def __init__(self, data=None, header=None, do_not_scale_image_data=False,
                 uint=False):
        """
        Construct a primary HDU.

        Parameters
        ----------
        data : array or DELAYED, optional
            The data in the HDU.

        header : Header instance, optional
            The header to be used (as a template).  If `header` is
            `None`, a minimal header will be provided.

        do_not_scale_image_data : bool, optional
            If `True`, image data is not scaled using BSCALE/BZERO values
            when read.

        uint : bool, optional
            Interpret signed integer data where ``BZERO`` is the
            central value and ``BSCALE == 1`` as unsigned integer
            data.  For example, `int16` data with ``BZERO = 32768``
            and ``BSCALE = 1`` would be treated as `uint16` data.
        """

        super(PrimaryHDU, self).__init__(
            data=data, header=header,
            do_not_scale_image_data=do_not_scale_image_data, uint=uint)
        self.name = 'PRIMARY'
        self._extver = 1

        # insert the keywords EXTEND
        if header is None:
            dim = self._header['NAXIS']
            if dim == 0:
                dim = ''
            self._header.set('EXTEND', True, after='NAXIS' + str(dim))

    @classmethod
    def match_header(cls, header):
        card = header.cards[0]
        return (card.keyword == 'SIMPLE' and
                ('GROUPS' not in header or header['GROUPS'] != True) and
                card.value == True)

    def update_header(self):
        super(PrimaryHDU, self).update_header()

        # Update the position of the EXTEND keyword if it already exists
        if 'EXTEND' in self._header:
            if len(self._axes):
                after = 'NAXIS' + str(len(self._axes))
            else:
                after = 'NAXIS'
            self._header.set('EXTEND', after=after)

    def _verify(self, option='warn'):
        errs = super(PrimaryHDU, self)._verify(option=option)

        # Verify location and value of mandatory keywords.
        # The EXTEND keyword is only mandatory if the HDU has extensions; this
        # condition is checked by the HDUList object.  However, if we already
        # have an EXTEND keyword check that its position is correct
        if 'EXTEND' in self._header:
            naxis = self._header.get('NAXIS', 0)
            self.req_cards('EXTEND', naxis + 3, lambda v: isinstance(v, bool),
                           True, option, errs)
        return errs


class ImageHDU(_ImageBaseHDU, ExtensionHDU):
    """
    FITS image extension HDU class.
    """

    _extension = 'IMAGE'

    def __init__(self, data=None, header=None, name=None,
                 do_not_scale_image_data=False, uint=False):
        """
        Construct an image HDU.

        Parameters
        ----------
        data : array
            The data in the HDU.

        header : Header instance
            The header to be used (as a template).  If `header` is
            `None`, a minimal header will be provided.

        name : str, optional
            The name of the HDU, will be the value of the keyword
            ``EXTNAME``.

        do_not_scale_image_data : bool, optional
            If `True`, image data is not scaled using BSCALE/BZERO values
            when read.

        uint : bool, optional
            Interpret signed integer data where ``BZERO`` is the
            central value and ``BSCALE == 1`` as unsigned integer
            data.  For example, `int16` data with ``BZERO = 32768``
            and ``BSCALE = 1`` would be treated as `uint16` data.
        """

        super(ImageHDU, self).__init__(
            data=data, header=header, name=name,
            do_not_scale_image_data=do_not_scale_image_data, uint=uint)

    @classmethod
    def match_header(cls, header):
        card = header.cards[0]
        xtension = card.value
        if isinstance(xtension, basestring):
            xtension = xtension.rstrip()
        return card.keyword == 'XTENSION' and xtension == cls._extension

    def _verify(self, option='warn'):
        """
        ImageHDU verify method.
        """

        errs = super(ImageHDU, self)._verify(option=option)
        naxis = self._header.get('NAXIS', 0)
        # PCOUNT must == 0, GCOUNT must == 1; the former is verifed in
        # ExtensionHDU._verify, however ExtensionHDU._verify allows PCOUNT
        # to be >= 0, so we need to check it here
        self.req_cards('PCOUNT', naxis + 3, lambda v: (_is_int(v) and v == 0),
                       0, option, errs)
        return errs


def _iswholeline(indx, naxis):
    if _is_int(indx):
        if indx >= 0 and indx < naxis:
            if naxis > 1:
                return _SinglePoint(1, indx)
            elif naxis == 1:
                return _OnePointAxis(1, 0)
        else:
            raise IndexError('Index %s out of range.' % indx)
    elif isinstance(indx, slice):
        indx = _normalize_slice(indx, naxis)
        if (indx.start == 0) and (indx.stop == naxis) and (indx.step == 1):
            return _WholeLine(naxis, 0)
        else:
            if indx.step == 1:
                return _LineSlice(indx.stop - indx.start, indx.start)
            else:
                return _SteppedSlice((indx.stop - indx.start) // indx.step,
                                     indx.start)
    else:
        raise IndexError('Illegal index %s' % indx)


class _KeyType(object):
    def __init__(self, npts, offset):
        self.npts = npts
        self.offset = offset


class _WholeLine(_KeyType):
    pass


class _SinglePoint(_KeyType):
    pass


class _OnePointAxis(_KeyType):
    pass


class _LineSlice(_KeyType):
    pass


class _SteppedSlice(_KeyType):
    pass
