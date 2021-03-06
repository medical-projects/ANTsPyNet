
import numpy as np
import ants

def sysu_media_wmh_segmentation(flair,
                                t1=None,
                                do_preprocessing=True,
                                use_ensemble=True,
                                use_axial_slices_only=True,
                                antsxnet_cache_directory=None,
                                verbose=False):

    """
    Perform WMH segmentation using the winning submission in the MICCAI
    2017 challenge by the sysu_media team using FLAIR or T1/FLAIR.  The
    MICCAI challenge is discussed in

    https://pubmed.ncbi.nlm.nih.gov/30908194/

    with the sysu_media's team entry is discussed in

     https://pubmed.ncbi.nlm.nih.gov/30125711/

    with the original implementation available here:

    https://github.com/hongweilibran/wmh_ibbmTum

    Arguments
    ---------
    flair : ANTsImage
        input 3-D FLAIR brain image (not skull-stripped).

    t1 : ANTsImage
        input 3-D T1 brain image (not skull-stripped).

    do_preprocessing : boolean
        perform n4 bias correction?

    use_ensemble : boolean
        check whether to use all 3 sets of weights.

    use_axial_slices_only : boolean
        If True, use original implementation which was trained on axial slices.
        If False, use ANTsXNet variant implementation which applies the slice-by-slice
        models to all 3 dimensions and averages the results.

    antsxnet_cache_directory : string
        Destination directory for storing the downloaded template and model weights.
        Since these can be resused, if is None, these data will be downloaded to a
        ~/.keras/ANTsXNet/.

    verbose : boolean
        Print progress to the screen.

    Returns
    -------
    WMH segmentation probability image

    Example
    -------
    >>> image = ants.image_read("flair.nii.gz")
    >>> probability_mask = sysu_media_wmh_segmentation(image)
    """

    from ..architectures import create_sysu_media_unet_model_2d
    from ..utilities import brain_extraction
    from ..utilities import crop_image_center
    from ..utilities import get_pretrained_network
    from ..utilities import preprocess_brain_image
    from ..utilities import pad_or_crop_image_to_size

    if flair.dimension != 3:
        raise ValueError( "Image dimension must be 3." )

    if antsxnet_cache_directory == None:
        antsxnet_cache_directory = "ANTsXNet"

    ################################
    #
    # Preprocess images
    #
    ################################

    flair_preprocessed = flair
    if do_preprocessing == True:
        flair_preprocessing = preprocess_brain_image(flair,
            truncate_intensity=(0.01, 0.99),
            do_brain_extraction=False,
            do_bias_correction=True,
            do_denoising=False,
            antsxnet_cache_directory=antsxnet_cache_directory,
            verbose=verbose)
        flair_preprocessed = flair_preprocessing["preprocessed_image"]

    number_of_channels = 1
    if t1 is not None:
        t1_preprocessed = t1
        if do_preprocessing == True:
            t1_preprocessing = preprocess_brain_image(t1,
                truncate_intensity=(0.01, 0.99),
                do_brain_extraction=False,
                do_bias_correction=True,
                do_denoising=False,
                antsxnet_cache_directory=antsxnet_cache_directory,
                verbose=verbose)
            t1_preprocessed = t1_preprocessing["preprocessed_image"]
        number_of_channels = 2

    ################################
    #
    # Estimate mask
    #
    ################################

    brain_mask = None
    if verbose == True:
        print("Estimating brain mask.")
    if t1 is not None:
        brain_mask = brain_extraction(t1, modality="t1")
    else:
        brain_mask = brain_extraction(flair, modality="flair")

    reference_image = ants.make_image((200, 200, 200),
                                      voxval=1,
                                      spacing=(1, 1, 1),
                                      origin=(0, 0, 0),
                                      direction=np.identity(3))

    center_of_mass_reference = ants.get_center_of_mass(reference_image)
    center_of_mass_image = ants.get_center_of_mass(brain_mask)
    translation = np.asarray(center_of_mass_image) - np.asarray(center_of_mass_reference)
    xfrm = ants.create_ants_transform(transform_type="Euler3DTransform",
        center=np.asarray(center_of_mass_reference), translation=translation)
    flair_preprocessed_warped = ants.apply_ants_transform_to_image(xfrm, flair_preprocessed, reference_image)
    brain_mask_warped = ants.threshold_image(
        ants.apply_ants_transform_to_image(xfrm, brain_mask, reference_image), 0.5, 1.1, 1, 0 )

    if t1 is not None:
        t1_preprocessed_warped = ants.apply_ants_transform_to_image(xfrm, t1_preprocessed, reference_image)

    ################################
    #
    # Gaussian normalize intensity based on brain mask
    #
    ################################

    mean_flair = flair_preprocessed_warped[brain_mask_warped > 0].mean()
    std_flair = flair_preprocessed_warped[brain_mask_warped > 0].std()
    flair_preprocessed_warped = (flair_preprocessed_warped - mean_flair) / std_flair

    if number_of_channels == 2:
        mean_t1 = t1_preprocessed_warped[brain_mask_warped > 0].mean()
        std_t1 = t1_preprocessed_warped[brain_mask_warped > 0].std()
        t1_preprocessed_warped = (t1_preprocessed_warped - mean_t1) / std_t1

    ################################
    #
    # Build models and load weights
    #
    ################################

    number_of_models = 1
    if use_ensemble == True:
        number_of_models = 3

    unet_models = list()
    for i in range(number_of_models):
        if number_of_channels == 1:
            weights_file_name = get_pretrained_network("sysuMediaWmhFlairOnlyModel" + str(i), antsxnet_cache_directory=antsxnet_cache_directory)
        else:
            weights_file_name = get_pretrained_network("sysuMediaWmhFlairT1Model" + str(i), antsxnet_cache_directory=antsxnet_cache_directory)
        unet_models.append(create_sysu_media_unet_model_2d((200, 200, number_of_channels)))
        unet_models[i].load_weights(weights_file_name)

    ################################
    #
    # Extract slices
    #
    ################################

    dimensions_to_predict = [2]
    if use_axial_slices_only == False:
        dimensions_to_predict = list(range(3))

    total_number_of_slices = 0
    for d in range(len(dimensions_to_predict)):
        total_number_of_slices += flair_preprocessed_warped.shape[dimensions_to_predict[d]]

    batchX = np.zeros((total_number_of_slices, 200, 200, number_of_channels))

    slice_count = 0
    for d in range(len(dimensions_to_predict)):
        number_of_slices = flair_preprocessed_warped.shape[dimensions_to_predict[d]]

        if verbose == True:
            print("Extracting slices for dimension ", dimensions_to_predict[d], ".")

        for i in range(number_of_slices):
            flair_slice = pad_or_crop_image_to_size(ants.slice_image(flair_preprocessed_warped, dimensions_to_predict[d], i), (200, 200))
            batchX[slice_count,:,:,0] = flair_slice.numpy()
            if number_of_channels == 2:
                t1_slice = pad_or_crop_image_to_size(ants.slice_image(t1_preprocessed_warped, dimensions_to_predict[d], i), (200, 200))
                batchX[slice_count,:,:,1] = t1_slice.numpy()

            slice_count += 1


    ################################
    #
    # Do prediction and then restack into the image
    #
    ################################

    if verbose == True:
        print("Prediction.")

    prediction = unet_models[0].predict(batchX, verbose=verbose)
    if number_of_models > 1:
       for i in range(1, number_of_models, 1):
           prediction += unet_models[i].predict(batchX, verbose=verbose)
    prediction /= number_of_models

    permutations = list()
    permutations.append((0, 1, 2))
    permutations.append((1, 0, 2))
    permutations.append((1, 2, 0))

    prediction_image_average = ants.image_clone(flair_preprocessed_warped) * 0

    current_start_slice = 0
    for d in range(len(dimensions_to_predict)):
        current_end_slice = current_start_slice + flair_preprocessed_warped.shape[dimensions_to_predict[d]] - 1
        which_batch_slices = range(current_start_slice, current_end_slice)
        prediction_per_dimension = prediction[which_batch_slices,:,:,:]
        prediction_array = np.transpose(np.squeeze(prediction_per_dimension), permutations[dimensions_to_predict[d]])
        prediction_image = ants.copy_image_info(flair_preprocessed_warped,
          pad_or_crop_image_to_size(ants.from_numpy(prediction_array),
            flair_preprocessed_warped.shape))
        prediction_image_average = prediction_image_average + (prediction_image - prediction_image_average) / (d + 1)
        current_start_slice = current_end_slice + 1

    probability_image = ants.apply_ants_transform_to_image(ants.invert_ants_transform(xfrm),
        prediction_image_average, flair)

    return(probability_image)

def ew_david(flair,
             t1,
             do_preprocessing=True,
             antsxnet_cache_directory=None,
             verbose=False):

    """
    Perform White matter hypterintensity probabilistic segmentation 
    using deep learning

    Preprocessing on the training data consisted of:
       * n4 bias correction,
       * brain extraction, and
       * affine registration to MNI.
    The input T1 should undergo the same steps.  If the input T1 is the raw
    T1, these steps can be performed by the internal preprocessing, i.e. set
    \code{doPreprocessing = TRUE}

    Arguments
    ---------
    flair : ANTsImage
        input 3-D FLAIR brain image (not skull-stripped).

    t1 : ANTsImage
        input 3-D T1 brain image (not skull-stripped).

    do_preprocessing : boolean
        perform n4 bias correction?

    verbose : boolean
        Print progress to the screen.

    Returns
    -------
    WMH segmentation probability image

    Example
    -------
    >>> image = ants.image_read("flair.nii.gz")
    >>> probability_mask = sysu_media_wmh_segmentation(image)
    """

    from ..architectures import create_unet_model_3d
    from ..utilities import get_pretrained_network
    from ..utilities import categorical_focal_loss
    from ..utilities import preprocess_brain_image
    from ..utilities import extract_image_patches
    from ..utilities import reconstruct_image_from_patches

    if flair.dimension != 3:
        raise ValueError( "Image dimension must be 3." )

    if t1.dimension != 3:
        raise ValueError( "Image dimension must be 3." )

    if antsxnet_cache_directory == None:
        antsxnet_cache_directory = "ANTsXNet"

    ################################
    #
    # Preprocess images
    #
    ################################

    t1_preprocessed = t1
    t1_preprocessing = None
    if do_preprocessing == True:
        t1_preprocessing = preprocess_brain_image(t1,
            truncate_intensity=(0.01, 0.99),
            do_brain_extraction=True,
            template="croppedMni152",
            template_transform_type="AffineFast",
            do_bias_correction=True,
            do_denoising=False,
            antsxnet_cache_directory=antsxnet_cache_directory,
            verbose=verbose)
        t1_preprocessed = t1_preprocessing["preprocessed_image"] * t1_preprocessing['brain_mask']

    flair_preprocessed = flair
    if do_preprocessing == True:
        flair_preprocessing = preprocess_brain_image(flair,
            truncate_intensity=(0.01, 0.99),
            do_brain_extraction=False,
            do_bias_correction=True,
            do_denoising=False,
            antsxnet_cache_directory=antsxnet_cache_directory,
            verbose=verbose)
        flair_preprocessed = ants.apply_transforms(fixed=t1_preprocessed, 
            moving=flair_preprocessing["preprocessed_image"], 
            transformlist=t1_preprocessing['template_transforms']['fwdtransforms'])
        flair_preprocessed = flair_preprocessed * t1_preprocessing['brain_mask']

    ################################
    #
    # Build model and load weights
    #
    ################################

    patch_size = (112, 112, 112)
    stride_length = (t1_preprocessed.shape[0] - patch_size[0],
                     t1_preprocessed.shape[1] - patch_size[1],
                     t1_preprocessed.shape[2] - patch_size[2])

    classes = ("background", "wmh" )
    number_of_classification_labels = len(classes)
    labels = (0, 1)

    image_modalities = ("T1", "FLAIR")
    channel_size = len(image_modalities)

    unet_model = create_unet_model_3d((*patch_size, channel_size),
        number_of_outputs = number_of_classification_labels,
        number_of_layers = 4, number_of_filters_at_base_layer = 16, dropout_rate = 0.0,
        convolution_kernel_size = (3, 3, 3), deconvolution_kernel_size = (2, 2, 2),
        weight_decay = 1e-5, nn_unet_activation_style=False, add_attention_gating=True)

    weights_file_name = get_pretrained_network("ewDavidWmhSegmentationWeights", 
        antsxnet_cache_directory=antsxnet_cache_directory)
    unet_model.load_weights(weights_file_name)

    ################################
    #
    # Do prediction and normalize to native space
    #
    ################################

    if verbose == True:
        print("ew_david:  prediction.")

    batchX = np.zeros((8, *patch_size, channel_size))

    t1_preprocessed = (t1_preprocessed - t1_preprocessed.mean()) / t1_preprocessed.std()
    t1_patches = extract_image_patches(t1_preprocessed, patch_size=patch_size,
                                          max_number_of_patches="all", stride_length=stride_length,
                                          return_as_array=True)
    batchX[:,:,:,:,0] = t1_patches

    flair_preprocessed = (flair_preprocessed - flair_preprocessed.mean()) / flair_preprocessed.std()
    flair_patches = extract_image_patches(flair_preprocessed, patch_size=patch_size,
                                        max_number_of_patches="all", stride_length=stride_length,
                                        return_as_array=True)
    batchX[:,:,:,:,1] = flair_patches

    predicted_data = unet_model.predict(batchX, verbose=verbose)

    probability_images = list()
    for i in range(len(labels)):
        print("Reconstructing image", classes[i])
        reconstructed_image = reconstruct_image_from_patches(predicted_data[:,:,:,:,i],
            domain_image=t1_preprocessed, stride_length=stride_length)

        if do_preprocessing == True:
            probability_images.append(ants.apply_transforms(fixed=t1,
                moving=reconstructed_image,
                transformlist=t1_preprocessing['template_transforms']['invtransforms'],
                whichtoinvert=[True], interpolator="linear", verbose=verbose))
        else:
            probability_images.append(reconstructed_image)

    return(probability_images[1])

