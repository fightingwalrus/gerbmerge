#!/usr/bin/env python
"""
Merge several RS274X (Gerber) files generated by Eagle into a single
job.

This program expects that each separate job has at least three files:
  - a board outline (RS274X)
  - data layers (copper, silkscreen, etc. in RS274X format)
  - an Excellon drill file

Furthermore, it is expected that each job was generated by Eagle
using the GERBER_RS274X plotter, except for the drill file which
was generated by the EXCELLON plotter.

This program places all jobs into a single job.

--------------------------------------------------------------------

This program is licensed under the GNU General Public License (GPL)
Version 3.  See http://www.fsf.org for details of the license.

Rugged Circuits LLC
http://ruggedcircuits.com/gerbmerge
"""

import sys
import os
import getopt
import re
import csv

import aptable
import jobs
import config
import parselayout
import fabdrawing
import strokes
import tiling
import tilesearch1
import tilesearch2
import placement
import schwartz
import util
import scoring
import drillcluster

VERSION_MAJOR=1
VERSION_MINOR=8

RANDOM_SEARCH = 1
EXHAUSTIVE_SEARCH = 2
FROM_FILE = 3
config.AutoSearchType = RANDOM_SEARCH
config.RandomSearchExhaustiveJobs = 2
config.PlacementFile = None

# This is a handle to a GUI front end, if any, else None for command-line usage
GUI = None

def usage():
  print \
"""
Usage: gerbmerge [Options] configfile [layoutfile]

Options:
    -h, --help          -- This help summary
    -v, --version       -- Program version and contact information
    --random-search     -- Automatic placement using random search (default)
    --full-search       -- Automatic placement using exhaustive search
    --place-file=fn     -- Read placement from file
    --rs-fsjobs=N       -- When using random search, exhaustively search N jobs
                           for each random placement (default: N=2)
    --search-timeout=T  -- When using random search, search for T seconds for best 
                           random placement (default: T=0, search until stopped)
    --no-trim-gerber    -- Do not attempt to trim Gerber data to extents of board
    --no-trim-excellon  -- Do not attempt to trim Excellon data to extents of board
    --octagons=fmt      -- Generate octagons in two different styles depending on
                           the value of 'fmt':

                              fmt is 'rotate' :  0.0 rotation
                              fmt is 'normal' : 22.5 rotation (default)

If a layout file is not specified, automatic placement is performed. If the
placement is read from a file, then no automatic placement is performed and
the layout file (if any) is ignored.

NOTE: The dimensions of each job are determined solely by the maximum extent of
the board outline layer for each job.
"""
  sys.exit(1)

def writeGerberHeader22degrees(fid):
  fid.write( \
"""G75*
G70*
%OFA0B0*%
%FSLAX25Y25*%
%IPPOS*%
%LPD*%
%AMOC8*
5,1,8,0,0,1.08239X$1,22.5*
%
""")

def writeGerberHeader0degrees(fid):
  fid.write( \
"""G75*
G70*
%OFA0B0*%
%FSLAX25Y25*%
%IPPOS*%
%LPD*%
%AMOC8*
5,1,8,0,0,1.08239X$1,0.0*
%
""")

writeGerberHeader = writeGerberHeader22degrees

def writeApertureMacros(fid, usedDict):
  keys = config.GAMT.keys()
  keys.sort()
  for key in keys:
    if key in usedDict:
      config.GAMT[key].writeDef(fid)

def writeApertures(fid, usedDict):
  keys = config.GAT.keys()
  keys.sort()
  for key in keys:
    if key in usedDict:
      config.GAT[key].writeDef(fid)

def writeGerberFooter(fid):
  fid.write('M02*\n')

def writeExcellonHeader(fid):
  fid.write('%\n')

def writeExcellonFooter(fid):
  fid.write('M30\n')

def writeExcellonTool(fid, tool, size):
  fid.write('%sC%f\n' % (tool, size))

def writeFiducials(fid, drawcode, OriginX, OriginY, MaxXExtent, MaxYExtent):
  """Place fiducials at arbitrary points. The FiducialPoints list in the config specifies
  sets of X,Y co-ordinates. Positive values of X/Y represent offsets from the lower left
  of the panel. Negative values of X/Y represent offsets from the top right. So:
         FiducialPoints = 0.125,0.125,-0.125,-0.125
  means to put a fiducial 0.125,0.125 from the lower left and 0.125,0.125 from the top right"""
  fid.write('%s*\n' % drawcode)    # Choose drawing aperture

  fList = config.Config['fiducialpoints'].split(',')
  for i in range(0, len(fList), 2):
    x,y = float(fList[i]), float(fList[i+1])
    if x>=0:
      x += OriginX
    else:
      x = MaxXExtent + x
    if y>=0:
      y += OriginX
    else:
      y = MaxYExtent + y
    fid.write('X%07dY%07dD03*\n' % (util.in2gerb(x), util.in2gerb(y)))

def writeCropMarks(fid, drawing_code, OriginX, OriginY, MaxXExtent, MaxYExtent):
  """Add corner crop marks on the given layer"""

  # Draw 125mil lines at each corner, with line edge right up against
  # panel border. This means the center of the line is D/2 offset
  # from the panel border, where D is the drawing line diameter.
  fid.write('%s*\n' % drawing_code)    # Choose drawing aperture

  offset = config.GAT[drawing_code].dimx/2.0

  # Lower-left
  x = OriginX + offset
  y = OriginY + offset
  fid.write('X%07dY%07dD02*\n' % (util.in2gerb(x+0.125), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.125)))

  # Lower-right
  x = MaxXExtent - offset
  y = OriginY + offset
  fid.write('X%07dY%07dD02*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.125)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x-0.125), util.in2gerb(y+0.000)))

  # Upper-right
  x = MaxXExtent - offset
  y = MaxYExtent - offset
  fid.write('X%07dY%07dD02*\n' % (util.in2gerb(x-0.125), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y-0.125)))

  # Upper-left
  x = OriginX + offset
  y = MaxYExtent - offset
  fid.write('X%07dY%07dD02*\n' % (util.in2gerb(x+0.000), util.in2gerb(y-0.125)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.000), util.in2gerb(y+0.000)))
  fid.write('X%07dY%07dD01*\n' % (util.in2gerb(x+0.125), util.in2gerb(y+0.000)))

def writeCentroidToCsv(fullname):
  # Write centroid data to csv.
  writer = csv.writer(open(fullname, 'wb'))
  writer.writerow(['RefDes', 'Layer', 'LocationX', 'LocationY', 'Rotation'])
  for key in sorted(config.CentroidPartMap):
    writer.writerows(config.CentroidPartMap[key])

def disclaimer():
  print """
****************************************************
*           R E A D    C A R E F U L L Y           *
*                                                  *
* This program comes with no warranty. You use     *
* this program at your own risk. Do not submit     *
* board files for manufacture until you have       *
* thoroughly inspected the output of this program  *
* using a previewing program such as:              *
*                                                  *
* Windows:                                         *
*          - GC-Prevue <http://www.graphicode.com> *
*          - ViewMate  <http://www.pentalogix.com> *
*                                                  *
* Linux:                                           *
*          - gerbv <http://gerbv.sourceforge.net>  *
*                                                  *
* By using this program you agree to take full     *
* responsibility for the correctness of the data   *
* that is generated by this program.               *
****************************************************

To agree to the above terms, press 'y' then Enter.
Any other key will exit the program.

"""

  s = raw_input()
  if s == 'y':
    print
    return

  print "\nExiting..."
  sys.exit(0)

def tile_jobs(Jobs):
  """Take a list of raw Job objects and find best tiling by calling tile_search"""

  # We must take the raw jobs and construct a list of 4-tuples (Xdim,Ydim,job,rjob).
  # This means we must construct a rotated job for each entry. We first sort all
  # jobs from largest to smallest. This should give us the best tilings first so
  # we can interrupt the tiling process and get a decent layout.
  L = []
  #sortJobs = schwartz.schwartz(Jobs, jobs.Job.jobarea)
  sortJobs = schwartz.schwartz(Jobs, jobs.Job.maxdimension)
  sortJobs.reverse()

  for job in sortJobs:
    Xdim = job.width_in()
    Ydim = job.height_in()
    rjob = jobs.rotateJob(job, 90)  ##NOTE: This will only try 90 degree rotations though 180 & 270 are available

    for count in range(job.Repeat):
      L.append( (Xdim,Ydim,job,rjob) )

  PX,PY = config.Config['panelwidth'],config.Config['panelheight']
  if config.AutoSearchType==RANDOM_SEARCH:
    tile = tilesearch2.tile_search2(L, PX, PY)
  else:
    tile = tilesearch1.tile_search1(L, PX, PY)

  if not tile:
    raise RuntimeError, 'Panel size %.2f"x%.2f" is too small to hold jobs' % (PX,PY)

  return tile

def merge(opts, args, gui = None):
  writeGerberHeader = writeGerberHeader22degrees
  
  global GUI
  GUI = gui
  
  for opt, arg in opts:
    if opt in ('--octagons',):
      if arg=='rotate':
        writeGerberHeader = writeGerberHeader0degrees
      elif arg=='normal':
        writeGerberHeader = writeGerberHeader22degrees
      else:
        raise RuntimeError, 'Unknown octagon format'
    elif opt in ('--random-search',):
      config.AutoSearchType = RANDOM_SEARCH
    elif opt in ('--full-search',):
      config.AutoSearchType = EXHAUSTIVE_SEARCH
    elif opt in ('--rs-fsjobs',):
      config.RandomSearchExhaustiveJobs = int(arg)
    elif opt in ('--search-timeout',):
      config.SearchTimeout = int(arg)
    elif opt in ('--place-file',):
      config.AutoSearchType = FROM_FILE
      config.PlacementFile = arg
    elif opt in ('--no-trim-gerber',):
      config.TrimGerber = 0
    elif opt in ('--no-trim-excellon',):
      config.TrimExcellon = 0
    else:
      raise RuntimeError, "Unknown option: %s" % opt

  if len(args) > 2 or len(args) < 1:
    raise RuntimeError, 'Invalid number of arguments'
    
  # Load up the Jobs global dictionary, also filling out GAT, the
  # global aperture table and GAMT, the global aperture macro table.
  updateGUI("Reading job files...")
  config.parseConfigFile(args[0])

  # Force all X and Y coordinates positive by adding absolute value of minimum X and Y
  for name, job in config.Jobs.iteritems():
    min_x, min_y = job.mincoordinates()
    shift_x = shift_y = 0
    if min_x < 0: shift_x = abs(min_x)
    if min_y < 0: shift_y = abs(min_y)
    if (shift_x > 0) or (shift_y > 0):
      job.fixcoordinates( shift_x, shift_y )

  # Display job properties                                                                
  for job in config.Jobs.values():
    print 'Job %s:' % job.name,
    if job.Repeat > 1:
      print '(%d instances)' % job.Repeat
    else:
      print
    print '  Extents: (%d,%d)-(%d,%d)' % (job.minx,job.miny,job.maxx,job.maxy)
    print '  Size: %f" x %f"' % (job.width_in(), job.height_in())
    print

  # Trim drill locations and flash data to board extents
  if config.TrimExcellon:
    updateGUI("Trimming Excellon data...")
    print 'Trimming Excellon data to board outlines ...'
    for job in config.Jobs.values():
      job.trimExcellon()

  if config.TrimGerber:
    updateGUI("Trimming Gerber data...")
    print 'Trimming Gerber data to board outlines ...'
    for job in config.Jobs.values():
      job.trimGerber()

  # We start origin at (0.1", 0.1") just so we don't get numbers close to 0
  # which could trip up Excellon leading-0 elimination.
  OriginX = OriginY = 0.1

  # Read the layout file and construct the nested list of jobs. If there
  # is no layout file, do auto-layout.
  updateGUI("Performing layout...")
  print 'Performing layout ...'
  if len(args) > 1:
    Layout = parselayout.parseLayoutFile(args[1])

    # Do the layout, updating offsets for each component job.
    X = OriginX + config.Config['leftmargin']
    Y = OriginY + config.Config['bottommargin']

    for row in Layout:
      row.setPosition(X, Y)
      Y += row.height_in() + config.Config['yspacing']

    # Construct a canonical placement from the layout
    Place = placement.Placement()
    Place.addFromLayout(Layout)

    del Layout

  elif config.AutoSearchType == FROM_FILE:
    Place = placement.Placement()
    Place.addFromFile(config.PlacementFile, config.Jobs)
  else:
    # Do an automatic layout based on our tiling algorithm.
    tile = tile_jobs(config.Jobs.values())

    Place = placement.Placement()
    Place.addFromTiling(tile, OriginX + config.Config['leftmargin'], OriginY + config.Config['bottommargin'])

  (MaxXExtent,MaxYExtent) = Place.extents()
  MaxXExtent += config.Config['rightmargin']
  MaxYExtent += config.Config['topmargin']

  # Start printing out the Gerbers. In preparation for drawing cut marks
  # and crop marks, make sure we have an aperture to draw with. Use a 10mil line.
  # If we're doing a fabrication drawing, we'll need a 1mil line.
  OutputFiles = []

  try:
    fullname = config.MergeOutputFiles['placement']
  except KeyError:
    fullname = 'merged.placement.txt'
  Place.write(fullname)
  OutputFiles.append(fullname)

  # For cut lines
  AP = aptable.Aperture(aptable.Circle, 'D??', config.Config['cutlinewidth'])
  drawing_code_cut = aptable.findInApertureTable(AP)
  if drawing_code_cut is None:
    drawing_code_cut = aptable.addToApertureTable(AP)

  # For crop marks
  AP = aptable.Aperture(aptable.Circle, 'D??', config.Config['cropmarkwidth'])
  drawing_code_crop = aptable.findInApertureTable(AP)
  if drawing_code_crop is None:
    drawing_code_crop = aptable.addToApertureTable(AP)

  # For fiducials
  drawing_code_fiducial_copper = drawing_code_fiducial_soldermask = None
  if config.Config['fiducialpoints']:
    AP = aptable.Aperture(aptable.Circle, 'D??', config.Config['fiducialcopperdiameter'])
    drawing_code_fiducial_copper = aptable.findInApertureTable(AP)
    if drawing_code_fiducial_copper is None:
      drawing_code_fiducial_copper = aptable.addToApertureTable(AP)
    AP = aptable.Aperture(aptable.Circle, 'D??', config.Config['fiducialmaskdiameter'])
    drawing_code_fiducial_soldermask = aptable.findInApertureTable(AP)
    if drawing_code_fiducial_soldermask is None:
      drawing_code_fiducial_soldermask = aptable.addToApertureTable(AP)

  # For fabrication drawing.
  AP = aptable.Aperture(aptable.Circle, 'D??', 0.001)
  drawing_code1 = aptable.findInApertureTable(AP)
  if drawing_code1 is None:
    drawing_code1 = aptable.addToApertureTable(AP)

  updateGUI("Writing merged files...")
  print 'Writing merged output files ...'

  for layername in config.LayerList.keys():
    if layername == 'centroid':
      continue

    lname = layername
    if lname[0]=='*':
      lname = lname[1:]

    try:
      fullname = config.MergeOutputFiles[layername]
    except KeyError:
      fullname = 'merged.%s.ger' % lname
    OutputFiles.append(fullname)
    #print 'Writing %s ...' % fullname
    fid = file(fullname, 'wt')
    writeGerberHeader(fid)
    
    # Determine which apertures and macros are truly needed
    apUsedDict = {}
    apmUsedDict = {}
    for job in Place.jobs:
      apd, apmd = job.aperturesAndMacros(layername)
      apUsedDict.update(apd)
      apmUsedDict.update(apmd)

    # Increase aperature sizes to match minimum feature dimension                         
    if config.MinimumFeatureDimension.has_key(layername):
    
      print '  Thickening', lname, 'feature dimensions ...'
      
      # Fix each aperture used in this layer
      for ap in apUsedDict.keys():
        new = config.GAT[ap].getAdjusted( config.MinimumFeatureDimension[layername] )
        if not new: ## current aperture size met minimum requirement
          continue
        else:       ## new aperture was created
          new_code = aptable.findOrAddAperture(new) ## get name of existing aperture or create new one if needed
          del apUsedDict[ap]                        ## the old aperture is no longer used in this layer
          apUsedDict[new_code] = None               ## the new aperture will be used in this layer
     
          # Replace all references to the old aperture with the new one
          for joblayout in Place.jobs:
            job = joblayout.job ##access job inside job layout 
            temp = []
            if job.hasLayer(layername):
              for x in job.commands[layername]:
                if x == ap:
                  temp.append(new_code) ## replace old aperture with new one
                else:
                  temp.append(x)        ## keep old command
              job.commands[layername] = temp

    if config.Config['cutlinelayers'] and (layername in config.Config['cutlinelayers']):
      apUsedDict[drawing_code_cut]=None

    if config.Config['cropmarklayers'] and (layername in config.Config['cropmarklayers']):
      apUsedDict[drawing_code_crop]=None
      
    if config.Config['fiducialpoints']:
      if ((layername=='*toplayer') or (layername=='*bottomlayer')):
        apUsedDict[drawing_code_fiducial_copper] = None
      elif ((layername=='*topsoldermask') or (layername=='*bottomsoldermask')):
        apUsedDict[drawing_code_fiducial_soldermask] = None

    # Write only necessary macro and aperture definitions to Gerber file
    writeApertureMacros(fid, apmUsedDict)
    writeApertures(fid, apUsedDict)

    #for row in Layout:
    #  row.writeGerber(fid, layername)

    #  # Do cut lines
    #  if config.Config['cutlinelayers'] and (layername in config.Config['cutlinelayers']):
    #    fid.write('%s*\n' % drawing_code_cut)    # Choose drawing aperture
    #    row.writeCutLines(fid, drawing_code_cut, OriginX, OriginY, MaxXExtent, MaxYExtent)

    # Finally, write actual flash data
    for job in Place.jobs:
    
      updateGUI("Writing merged output files...")
      job.writeGerber(fid, layername)

      if config.Config['cutlinelayers'] and (layername in config.Config['cutlinelayers']):
        fid.write('%s*\n' % drawing_code_cut)    # Choose drawing aperture
        job.writeCutLines(fid, drawing_code_cut, OriginX, OriginY, MaxXExtent, MaxYExtent)

    if config.Config['cropmarklayers']:
      if layername in config.Config['cropmarklayers']:
        writeCropMarks(fid, drawing_code_crop, OriginX, OriginY, MaxXExtent, MaxYExtent)

    if config.Config['fiducialpoints']:
      if ((layername=='*toplayer') or (layername=='*bottomlayer')):
        writeFiducials(fid, drawing_code_fiducial_copper, OriginX, OriginY, MaxXExtent, MaxYExtent)
      elif ((layername=='*topsoldermask') or (layername=='*bottomsoldermask')):
        writeFiducials(fid, drawing_code_fiducial_soldermask, OriginX, OriginY, MaxXExtent, MaxYExtent)
      
    writeGerberFooter(fid)
    fid.close()

  # Write board outline layer if selected
  fullname = config.Config['outlinelayerfile']
  if fullname and fullname.lower() != "none":
    OutputFiles.append(fullname)
    #print 'Writing %s ...' % fullname
    fid = file(fullname, 'wt')
    writeGerberHeader(fid)

    # Write width-1 aperture to file
    AP = aptable.Aperture(aptable.Circle, 'D10', 0.001)
    AP.writeDef(fid)

    # Choose drawing aperture D10
    fid.write('D10*\n')

    # Draw the rectangle
    fid.write('X%07dY%07dD02*\n' % (util.in2gerb(OriginX), util.in2gerb(OriginY)))        # Bottom-left
    fid.write('X%07dY%07dD01*\n' % (util.in2gerb(OriginX), util.in2gerb(MaxYExtent)))     # Top-left
    fid.write('X%07dY%07dD01*\n' % (util.in2gerb(MaxXExtent), util.in2gerb(MaxYExtent)))  # Top-right
    fid.write('X%07dY%07dD01*\n' % (util.in2gerb(MaxXExtent), util.in2gerb(OriginY)))     # Bottom-right
    fid.write('X%07dY%07dD01*\n' % (util.in2gerb(OriginX), util.in2gerb(OriginY)))        # Bottom-left

    writeGerberFooter(fid)
    fid.close()

  # Write scoring layer if selected
  fullname = config.Config['scoringfile']
  if fullname and fullname.lower() != "none":
    OutputFiles.append(fullname)
    #print 'Writing %s ...' % fullname
    fid = file(fullname, 'wt')
    writeGerberHeader(fid)

    # Write width-1 aperture to file
    AP = aptable.Aperture(aptable.Circle, 'D10', 0.001)
    AP.writeDef(fid)

    # Choose drawing aperture D10
    fid.write('D10*\n')

    # Draw the scoring lines
    scoring.writeScoring(fid, Place, OriginX, OriginY, MaxXExtent, MaxYExtent)

    writeGerberFooter(fid)
    fid.close()

  # Get a list of all tools used by merging keys from each job's dictionary
  # of tools.
  if 0:
    Tools = {}
    for job in config.Jobs.values():
      for key in job.xcommands.keys():
        Tools[key] = 1

    Tools = Tools.keys()
    Tools.sort()
  else:
    toolNum = 0

    # First construct global mapping of diameters to tool numbers
    for job in config.Jobs.values():
      for tool,diam in job.xdiam.items():
        if config.GlobalToolRMap.has_key(diam):
          continue

        toolNum += 1
        config.GlobalToolRMap[diam] = "T%02d" % toolNum

    # Cluster similar tool sizes to reduce number of drills
    if config.Config['drillclustertolerance'] > 0:
      config.GlobalToolRMap = drillcluster.cluster( config.GlobalToolRMap, config.Config['drillclustertolerance'] )
      drillcluster.remap( Place.jobs, config.GlobalToolRMap.items() )

    # Now construct mapping of tool numbers to diameters
    for diam,tool in config.GlobalToolRMap.items():
      config.GlobalToolMap[tool] = diam

    # Tools is just a list of tool names
    Tools = config.GlobalToolMap.keys()
    Tools.sort()   

  fullname = config.Config['fabricationdrawingfile']
  if fullname and fullname.lower() != 'none':
    if len(Tools) > strokes.MaxNumDrillTools:
      raise RuntimeError, "Only %d different tool sizes supported for fabrication drawing." % strokes.MaxNumDrillTools

    OutputFiles.append(fullname)
    #print 'Writing %s ...' % fullname
    fid = file(fullname, 'wt')
    writeGerberHeader(fid)
    writeApertures(fid, {drawing_code1: None})
    fid.write('%s*\n' % drawing_code1)    # Choose drawing aperture

    fabdrawing.writeFabDrawing(fid, Place, Tools, OriginX, OriginY, MaxXExtent, MaxYExtent)

    writeGerberFooter(fid)
    fid.close()
    
  # Finally, print out the Excellon
  try:
    fullname = config.MergeOutputFiles['drills']
  except KeyError:
    fullname = 'merged.drills.xln'
  OutputFiles.append(fullname)
  #print 'Writing %s ...' % fullname
  fid = file(fullname, 'wt')

  writeExcellonHeader(fid)

  # Ensure each one of our tools is represented in the tool list specified
  # by the user.
  for tool in Tools:
    try:
      size = config.GlobalToolMap[tool]
    except:
      raise RuntimeError, "INTERNAL ERROR: Tool code %s not found in global tool map" % tool
      
    writeExcellonTool(fid, tool, size)

    #for row in Layout:
    #  row.writeExcellon(fid, size)
    for job in Place.jobs:
        job.writeExcellon(fid, size)
  
  writeExcellonFooter(fid)
  fid.close()

  # updates the global centroid map with rotated
  # and offset centroid data by part description
  for job in Place.jobs:
    job.writeCentroid()

  try:
    fullname = config.MergeOutputFiles['centroid']
  except KeyError:
    fullname = 'merged.centroid.smd'

  OutputFiles.append(fullname)
  writeCentroidToCsv(fullname)


  updateGUI("Closing files...")

  # Compute stats
  jobarea = 0.0
  #for row in Layout:
  #  jobarea += row.jobarea()
  for job in Place.jobs:
    jobarea += job.jobarea()
    
  totalarea = ((MaxXExtent-OriginX)*(MaxYExtent-OriginY))

  ToolStats = {}
  drillhits = 0
  for tool in Tools:
    ToolStats[tool]=0
    #for row in Layout:
    #  hits = row.drillhits(config.GlobalToolMap[tool])
    #  ToolStats[tool] += hits
    #  drillhits += hits
    for job in Place.jobs:
      hits = job.drillhits(config.GlobalToolMap[tool])
      ToolStats[tool] += hits
      drillhits += hits

  try:
    fullname = config.MergeOutputFiles['toollist']
  except KeyError:
    fullname = 'merged.toollist.drl'
  OutputFiles.append(fullname)
  #print 'Writing %s ...' % fullname
  fid = file(fullname, 'wt')

  print '-'*50
  print '     Job Size : %f" x %f"' % (MaxXExtent-OriginX, MaxYExtent-OriginY)
  print '     Job Area : %.2f sq. in.' % totalarea
  print '   Area Usage : %.1f%%' % (jobarea/totalarea*100)
  print '   Drill hits : %d' % drillhits
  print 'Drill density : %.1f hits/sq.in.' % (drillhits/totalarea)

  print '\nTool List:'
  smallestDrill = 999.9
  for tool in Tools:
    if ToolStats[tool]:
      fid.write('%s %.4fin\n' % (tool, config.GlobalToolMap[tool]))
      print '  %s %.4f" %5d hits' % (tool, config.GlobalToolMap[tool], ToolStats[tool])
      smallestDrill = min(smallestDrill, config.GlobalToolMap[tool])

  fid.close()
  print "Smallest Tool: %.4fin" % smallestDrill

  print
  print 'Output Files :'
  for f in OutputFiles:
    print '  ', f

  if (MaxXExtent-OriginX)>config.Config['panelwidth'] or (MaxYExtent-OriginY)>config.Config['panelheight']:
    print '*'*75
    print '*'
    print '* ERROR: Merged job exceeds panel dimensions of %.1f"x%.1f"' % (config.Config['panelwidth'],config.Config['panelheight'])
    print '*'
    print '*'*75
    sys.exit(1)

  # Done!
  return 0

def updateGUI(text = None):
  global GUI
  if GUI != None:
    GUI.updateProgress(text)

if __name__=="__main__":
  try:
    opts, args = getopt.getopt(sys.argv[1:], 'hv', ['help', 'version', 'octagons=', 'random-search', 'full-search', 'rs-fsjobs=', 'search-timeout=', 'place-file=', 'no-trim-gerber', 'no-trim-excellon'])
  except getopt.GetoptError:
    usage()
    
  for opt, arg in opts:
    if opt in ('-h', '--help'):
      usage()
    elif opt in ('-v', '--version'):
      print """
GerbMerge Version %d.%d  --  Combine multiple Gerber/Excellon files

This program is licensed under the GNU General Public License (GPL)
Version 3. See http://www.fsf.org for details of this license.

Rugged Circuits LLC
http://ruggedcircuits.com/gerbmerge
""" % (VERSION_MAJOR, VERSION_MINOR)
      sys.exit(0)
    elif opt in ('--octagons', '--random-search','--full-search','--rs-fsjobs','--place-file','--no-trim-gerber','--no-trim-excellon', '--search-timeout'):
      pass ## arguments are valid
    else:
      raise RuntimeError, "Unknown option: %s" % opt

  if len(args) > 2 or len(args) < 1:
    usage()

  disclaimer()
  
  sys.exit(merge(opts, args)) ## run germberge
# vim: expandtab ts=2 sw=2 ai syntax=python
