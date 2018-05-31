#!/usr/bin/env python

import argparse
import gdal
import glob
import numpy
import logging
import os
import re
import vtk
from vtk.numpy_interface import dataset_adapter as dsa
from vtk.util import numpy_support


def main(args):
    parser = argparse.ArgumentParser(
        description='Render a DSM from a DTM and polygons representing buildings.')
    parser.add_argument("input_buildings",
                        help="Input buildings polygonal file (.vtp) or folder "
                             "containing .obj files. Building object files start "
                             "with a digit, road object files start with \"Road\". "
                             "All obj files start with comments specifying the offsets "
                             "that are added the coordinats. There are three comment lines, "
                             "one for each coordinate: "
                             "\"#c offset: value\" where c is x, y and z.")
    parser.add_argument("input_dtm", help="Input digital terain model (DTM)")
    parser.add_argument("output_dsm", help="Output digital surface model (DSM)")
    parser.add_argument("--render_png", action="store_true",
                        help="Do not save the DSM, render into a PNG instead.")
    parser.add_argument("--render_cls", action="store_true",
                        help="Render a buildings mask: render buildings label (6), "
                             "background (2) and no DTM.")
    parser.add_argument("--buildings_only", action="store_true",
                        help="Do not use the DTM, use only the buildings.")
    parser.add_argument("--debug", action="store_true",
                        help="Save intermediate results")
    args = parser.parse_args(args)

    # open the DTM
    dtm = gdal.Open(args.input_dtm, gdal.GA_ReadOnly)
    if not dtm:
        raise RuntimeError("Error: Failed to open DTM {}".format(args.input_dtm))

    dtmDriver = dtm.GetDriver()
    dtmDriverMetadata = dtmDriver.GetMetadata()
    dsm = None
    dtmBounds = [0.0, 0.0, 0.0, 0.0]
    if dtmDriverMetadata.get(gdal.DCAP_CREATE) == "YES":
        print("Create destination image "
              "size:({}, {}) ...".format(dtm.RasterXSize,
                                         dtm.RasterYSize))
        # georeference information
        projection = dtm.GetProjection()
        transform = dtm.GetGeoTransform()
        gcpProjection = dtm.GetGCPProjection()
        gcps = dtm.GetGCPs()
        options = []
        # ensure that space will be reserved for geographic corner coordinates
        # (in DMS) to be set later
        if (dtmDriver.ShortName == "NITF" and not projection):
            options.append("ICORDS=G")
        if args.render_cls:
            eType = gdal.GDT_Byte
        else:
            eType = gdal.GDT_Float32
        dsm = dtmDriver.Create(
            args.output_dsm, xsize=dtm.RasterXSize,
            ysize=dtm.RasterYSize, bands=1, eType=eType,
            options=options)
        if (projection):
            # georeference through affine geotransform
            dsm.SetProjection(projection)
            dsm.SetGeoTransform(transform)
        else:
            # georeference through GCPs
            dsm.SetGCPs(gcps, gcpProjection)
            gdal.GCPsToGeoTransform(gcps, transform)
        corners = [[0, 0], [0, dtm.RasterYSize],
                   [dtm.RasterXSize, dtm.RasterYSize], [dtm.RasterXSize, 0]]
        geoCorners = numpy.zeros((4, 2))
        for i, corner in enumerate(corners):
            geoCorners[i] = [
                transform[0] + corner[0] * transform[1] +
                corner[1] * transform[2],
                transform[3] + corner[0] * transform[4] +
                corner[1] * transform[5]]
        dtmBounds[0] = numpy.min(geoCorners[:, 0])
        dtmBounds[1] = numpy.max(geoCorners[:, 0])
        dtmBounds[2] = numpy.min(geoCorners[:, 1])
        dtmBounds[3] = numpy.max(geoCorners[:, 1])

        if args.render_cls:
            # label for no building
            dtmRaster = numpy.full([dtm.RasterYSize, dtm.RasterXSize], 2)
            nodata = 0
        else:
            print("Reading the DTM {} size: ({}, {})\n"
                  "\tbounds: ({}, {}), ({}, {})...".format(
                      args.input_dtm, dtm.RasterXSize, dtm.RasterYSize,
                      dtmBounds[0], dtmBounds[1],
                      dtmBounds[2], dtmBounds[3]))
            dtmRaster = dtm.GetRasterBand(1).ReadAsArray()
            nodata = dtm.GetRasterBand(1).GetNoDataValue()
        print("Nodata: {}".format(nodata))
    else:
        raise RuntimeError("Driver {} does not supports Create().".format(dtmDriver))

    # read the buildings polydata, set Z as a scalar and project to XY plane
    print("Reading the buildings ...")
    # labels for buildings and elevated roads
    labels = [6, 17]
    if (os.path.isfile(args.input_buildings)):
        polyReader = vtk.vtkXMLPolyDataReader()
        polyReader.SetFileName(args.input_buildings)
        polyReader.Update()
        polyVtkList = [polyReader.GetOutput()]
    else:
        # assume a folder with OBJ files,
        # buildings start with numbers
        # optional elevated roads start with Road*.obj
        files = [glob.glob(args.input_buildings + "/[0-9]*.obj"),
                 glob.glob(args.input_buildings + "/Road*.obj")]
        files = [x for x in files if x]
        if len(files) >= 2:
            print("Found {} buildings and {} roads".format(len(files[0]), len(files[1])))
        elif len(files) == 1:
            print("Found {} buildings".format(len(files[0])))
        else:
            raise RuntimeError("No OBJ files found in {}".format(args.input_buildings))
        offset = [0.0, 0.0, 0.0]
        axes = ['x', 'y', 'z']
        reFloatList = list("#. offset: ([-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?)")
        with open(files[0][0]) as f:
            for i in range(3):
                reFloatList[1] = axes[i]
                reFloat = re.compile("".join(reFloatList))
                line = f.readline()
                match = reFloat.search(line)
                if match:
                    offset[i] = float(match.group(1))
                else:
                    break
        print("Offset: {}".format(offset))
        transform = vtk.vtkTransform()
        transform.Translate(offset[0], offset[1], offset[2])
        polyVtkList = []
        for category in range(len(files)):
            append = vtk.vtkAppendPolyData()
            for i, fileName in enumerate(files[category]):
                objReader = vtk.vtkOBJReader()
                objReader.SetFileName(fileName)
                transformFilter = vtk.vtkTransformFilter()
                transformFilter.SetTransform(transform)
                transformFilter.SetInputConnection(objReader.GetOutputPort())
                append.AddInputConnection(transformFilter.GetOutputPort())
            append.Update()
            polyVtkList.append(append.GetOutput())

    arrayName = "Elevation"
    append = vtk.vtkAppendPolyData()
    for category in range(len(polyVtkList)):
        poly = dsa.WrapDataObject(polyVtkList[category])
        polyElevation = poly.Points[:, 2]
        if args.render_cls:
            # label for buildings
            polyElevation[:] = labels[category]
        polyElevationVtk = numpy_support.numpy_to_vtk(polyElevation)
        polyElevationVtk.SetName(arrayName)
        poly.PointData.SetScalars(polyElevationVtk)
        append.AddInputDataObject(polyVtkList[category])
    append.Update()


    # Create the RenderWindow, Renderer
    ren = vtk.vtkRenderer()
    renWin = vtk.vtkRenderWindow()
    renWin.OffScreenRenderingOn()
    renWin.SetSize(dtm.RasterXSize, dtm.RasterYSize)
    renWin.SetMultiSamples(0)
    renWin.AddRenderer(ren)

    # show the buildings
    trisBuildingsFilter = vtk.vtkTriangleFilter()
    trisBuildingsFilter.SetInputDataObject(append.GetOutput())
    trisBuildingsFilter.Update()

    p2cBuildings = vtk.vtkPointDataToCellData()
    p2cBuildings.SetInputConnection(trisBuildingsFilter.GetOutputPort())
    p2cBuildings.PassPointDataOn()
    p2cBuildings.Update()
    buildingsScalarRange = p2cBuildings.GetOutput().GetCellData().GetScalars().GetRange()

    if (args.debug):
        polyWriter = vtk.vtkXMLPolyDataWriter()
        polyWriter.SetFileName("p2c.vtp")
        polyWriter.SetInputConnection(p2cBuildings.GetOutputPort())
        polyWriter.Write()

    buildingsMapper = vtk.vtkPolyDataMapper()
    buildingsMapper.SetInputDataObject(p2cBuildings.GetOutput())

    buildingsActor = vtk.vtkActor()
    buildingsActor.SetMapper(buildingsMapper)
    ren.AddActor(buildingsActor)

    if (args.render_png):
        print("Render into a PNG ...")
        # Show the terrain.
        print("Converting the DTM into a surface ...")
        # read the DTM as a VTK object
        dtmReader = vtk.vtkGDALRasterReader()
        dtmReader.SetFileName(args.input_dtm)
        dtmReader.Update()
        dtmVtk = dtmReader.GetOutput()

        # Convert the terrain into a polydata.
        surface = vtk.vtkImageDataGeometryFilter()
        surface.SetInputDataObject(dtmVtk)

        # Make sure the polygons are planar, so need to use triangles.
        tris = vtk.vtkTriangleFilter()
        tris.SetInputConnection(surface.GetOutputPort())

        # Warp the surface by scalar values
        warp = vtk.vtkWarpScalar()
        warp.SetInputConnection(tris.GetOutputPort())
        warp.SetScaleFactor(1)
        warp.UseNormalOn()
        warp.SetNormal(0, 0, 1)
        warp.Update()
        dsmScalarRange = warp.GetOutput().GetPointData().GetScalars().GetRange()

        dtmMapper = vtk.vtkPolyDataMapper()
        dtmMapper.SetInputConnection(warp.GetOutputPort())
        dtmActor = vtk.vtkActor()
        dtmActor.SetMapper(dtmMapper)
        ren.AddActor(dtmActor)

        ren.ResetCamera()
        camera = ren.GetActiveCamera()
        camera.ParallelProjectionOn()
        camera.SetParallelScale((dtmBounds[3] - dtmBounds[2])/2)

        if (args.buildings_only):
            scalarRange = buildingsScalarRange
        else:
            scalarRange = [min(dsmScalarRange[0], buildingsScalarRange[0]),
                           max(dsmScalarRange[1], buildingsScalarRange[1])]
        lut = vtk.vtkColorTransferFunction()
        lut.AddRGBPoint(scalarRange[0], 0.23, 0.30, 0.75)
        lut.AddRGBPoint((scalarRange[0] + scalarRange[1]) / 2, 0.86, 0.86, 0.86)
        lut.AddRGBPoint(scalarRange[1], 0.70, 0.02, 0.15)

        dtmMapper.SetLookupTable(lut)
        dtmMapper.SetColorModeToMapScalars()
        buildingsMapper.SetLookupTable(lut)
        if (args.buildings_only):
            ren.RemoveActor(dtmActor)

        renWin.Render()
        windowToImageFilter = vtk.vtkWindowToImageFilter()
        windowToImageFilter.SetInput(renWin)
        windowToImageFilter.SetInputBufferTypeToRGBA()
        windowToImageFilter.ReadFrontBufferOff()
        windowToImageFilter.Update()

        writerPng = vtk.vtkPNGWriter()
        writerPng.SetFileName(args.output_dsm + ".png")
        writerPng.SetInputConnection(windowToImageFilter.GetOutputPort())
        writerPng.Write()
    else:
        print("Render into a floating point buffer ...")

        ren.ResetCamera()
        camera = ren.GetActiveCamera()
        camera.ParallelProjectionOn()
        camera.SetParallelScale((dtmBounds[3] - dtmBounds[2])/2)
        distance = camera.GetDistance()
        focalPoint = [(dtmBounds[0] + dtmBounds[1]) * 0.5,
                      (dtmBounds[3] + dtmBounds[2]) * 0.5,
                      (buildingsScalarRange[0] + buildingsScalarRange[1]) * 0.5]
        position = [focalPoint[0], focalPoint[1], focalPoint[2] + distance]
        camera.SetFocalPoint(focalPoint)
        camera.SetPosition(position)

        valuePass = vtk.vtkValuePass()
        valuePass.SetRenderingMode(vtk.vtkValuePass.FLOATING_POINT)
        # use the default scalar for point data
        valuePass.SetInputComponentToProcess(0)
        valuePass.SetInputArrayToProcess(vtk.VTK_SCALAR_MODE_USE_POINT_FIELD_DATA,
                                         arrayName)
        passes = vtk.vtkRenderPassCollection()
        passes.AddItem(valuePass)
        sequence = vtk.vtkSequencePass()
        sequence.SetPasses(passes)
        cameraPass = vtk.vtkCameraPass()
        cameraPass.SetDelegatePass(sequence)
        ren.SetPass(cameraPass)
        # We have to render the points first, otherwise we get a segfault.
        renWin.Render()
        valuePass.SetInputArrayToProcess(vtk.VTK_SCALAR_MODE_USE_CELL_FIELD_DATA, arrayName)
        renWin.Render()
        elevationFlatVtk = valuePass.GetFloatImageDataArray(ren)
        valuePass.ReleaseGraphicsResources(renWin)

        print("Writing the DSM ...")
        elevationFlat = numpy_support.vtk_to_numpy(elevationFlatVtk)
        # VTK X,Y corresponds to numpy cols,rows. VTK stores arrays
        # in Fortran order.
        elevationTranspose = numpy.reshape(
            elevationFlat, [dtm.RasterXSize, dtm.RasterYSize], "F")
        # changes from cols, rows to rows,cols.
        elevation = numpy.transpose(elevationTranspose)
        # numpy rows increase as you go down, Y for VTK images increases as you go up
        elevation = numpy.flip(elevation, 0)
        if args.buildings_only:
            dsmElevation = elevation
        else:
            # elevation has nans in places other than buildings
            dsmElevation = numpy.fmax(dtmRaster, elevation)
        dsm.GetRasterBand(1).WriteArray(dsmElevation)
        if nodata:
            dsm.GetRasterBand(1).SetNoDataValue(nodata)


if __name__ == '__main__':
    import sys
    try:
        main(sys.argv[1:])
    except Exception as e:
        logging.exception(e)
        sys.exit(1)
