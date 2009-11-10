import subprocess, threading, os, errno, time, datetime
from Queue import Queue, Empty
from datetime import datetime

from galaxy import model # Database interaction class
from galaxy.model import mapping
from galaxy.datatypes.data import nice_size
from galaxy.util.bunch import Bunch
from Queue import Queue
from sqlalchemy import or_

import galaxy.eggs
galaxy.eggs.require("boto")
from boto.ec2.connection import EC2Connection
from boto.ec2.regioninfo import RegionInfo
import boto.exception
import boto

import logging
log = logging.getLogger( __name__ )

uci_states = Bunch(
    NEW_UCI = "newUCI",
    NEW = "new",
    DELETING_UCI = "deletingUCI",
    DELETING = "deleting",
    SUBMITTED_UCI = "submittedUCI",
    SUBMITTED = "submitted",
    SHUTTING_DOWN_UCI = "shutting-downUCI",
    SHUTTING_DOWN = "shutting-down",
    AVAILABLE = "available",
    RUNNING = "running",
    PENDING = "pending",
    ERROR = "error",
    DELETED = "deleted"
)

instance_states = Bunch(
    TERMINATED = "terminated",
    SUBMITTED = "submitted",
    RUNNING = "running",
    PENDING = "pending",
    SHUTTING_DOWN = "shutting-down",
    ERROR = "error"
)

store_states = Bunch(
    IN_USE = "in-use",
    CREATING = "creating",
    ERROR = "error"
)

class EC2CloudProvider( object ):
    """
    Amazon EC2-based cloud provider implementation for managing instances. 
    """
    STOP_SIGNAL = object()
    def __init__( self, app ):
        self.type = "ec2" # cloud provider type (e.g., ec2, eucalyptus, opennebula)
        self.zone = "us-east-1a"
        self.key_pair = "galaxy-keypair"
        self.security_group = "galaxyWeb"
        self.queue = Queue()
        
        self.threads = []
        nworkers = 5
        log.info( "Starting EC2 cloud controller workers..." )
        for i in range( nworkers  ):
            worker = threading.Thread( target=self.run_next )
            worker.start()
            self.threads.append( worker )
        log.debug( "%d EC2 cloud workers ready", nworkers )
        
    def run_next( self ):
        """Run the next job, waiting until one is available if necessary"""
        cnt = 0
        while 1:
            
            uci_wrapper = self.queue.get()
            uci_state = uci_wrapper.get_state()
            if uci_state is self.STOP_SIGNAL:
                return
            try:
                if uci_state==uci_states.NEW:
                    self.createUCI( uci_wrapper )
                elif uci_state==uci_states.DELETING:
                    self.deleteUCI( uci_wrapper )
                elif uci_state==uci_states.SUBMITTED:
                    self.startUCI( uci_wrapper )
                elif uci_state==uci_states.SHUTTING_DOWN:
                    self.stopUCI( uci_wrapper )
            except:
                log.exception( "Uncaught exception executing request." )
            cnt += 1
            
    def get_connection( self, uci_wrapper ):
        """
        Establishes EC2 cloud connection using user's credentials associated with given UCI
        """
        log.debug( 'Establishing %s cloud connection' % self.type )
        provider = uci_wrapper.get_provider()
        try:
            region = RegionInfo( None, provider.region_name, provider.region_endpoint )
        except Exception, e:
            log.error( "Selecting region with cloud provider failed: %s" % str(e) )
            uci_wrapper.set_error( "Selecting region with cloud provider failed: " + str(e), True )
            return None
        try:
            conn = EC2Connection( aws_access_key_id=uci_wrapper.get_access_key(), 
                                  aws_secret_access_key=uci_wrapper.get_secret_key(), 
                                  is_secure=provider.is_secure, 
                                  region=region, 
                                  path=provider.path )
        except Exception, e:
            log.error( "Establishing connection with cloud failed: %s" % str(e) )
            uci_wrapper.set_error( "Establishing connection with cloud failed: " + str(e), True )
            return None
        
        return conn
        
    def check_key_pair( self, uci_wrapper, conn ):
        """
        Generate key pair using user's credentials
        """
        kp = None
        kp_name = uci_wrapper.get_name().replace(' ','_') + "_kp"
        log.debug( "Checking user's key pair: '%s'" % kp_name )
        try:
            kp = conn.get_key_pair( kp_name )
            uci_kp_name = uci_wrapper.get_key_pair_name()
            uci_material = uci_wrapper.get_key_pair_material()
            if kp != None:
                if kp.name != uci_kp_name or uci_material == None:
                    # key pair exists on the cloud but not in local database, so re-generate it (i.e., delete and then create)
                    try: 
                        conn.delete_key_pair( kp_name )
                        kp = self.create_key_pair( conn, kp_name )
                        uci_wrapper.set_key_pair( kp.name, kp.material )
                    except boto.exception.EC2ResponseError, e:
                        log.error( "EC2 response error when deleting key pair: '%s'" % e )
                        uci_wrapper.set_error( "EC2 response error while deleting key pair: " + str( e ), True )
            else:
                try:
                    kp = self.create_key_pair( conn, kp_name )
                    uci_wrapper.set_key_pair( kp.name, kp.material )
                except boto.exception.EC2ResponseError, e:
                    log.error( "EC2 response error when creating key pair: '%s'" % e )
                    uci_wrapper.set_error( "EC2 response error while creating key pair: " + str( e ), True )
                except Exception, ex:
                    log.error( "Exception when creating key pair: '%s'" % e )
                    uci_wrapper.set_error( "Error while creating key pair: " + str( e ), True )
                
        except boto.exception.EC2ResponseError, e: # No keypair under this name exists so create it
            if e.code == 'InvalidKeyPair.NotFound': 
                log.info( "No keypair found, creating keypair '%s'" % kp_name )
                kp = self.create_key_pair( conn, kp_name )
                uci_wrapper.set_key_pair( kp.name, kp.material )
            else:
                log.error( "EC2 response error while retrieving key pair: '%s'" % e )
                uci_wrapper.set_error( "Cloud provider response error while retrieving key pair: " + str( e ), True )
                        
        if kp != None:
            return kp.name
        else:
            return None
    
    def create_key_pair( self, conn, kp_name ):
        try:
            return conn.create_key_pair( kp_name )
        except boto.exception.EC2ResponseError, e: 
            return None
    
    def get_mi_id( self, uci_wrapper, i_index ):
        """
        Get appropriate machine image (mi) based on instance size.
        """
        i_type = uci_wrapper.get_type( i_index )
        if i_type=='m1.small' or i_type=='c1.medium':
            arch = 'i386'
        else:
            arch = 'x86_64' 
        
        mi = model.CloudImage.filter_by( deleted=False, provider_type=self.type, architecture=arch ).first()
        if mi:
            return mi.image_id
        else:
            log.error( "Machine image could not be retrieved for UCI '%s'." % uci_wrapper.get_name() )
            uci_wrapper.set_error( "Machine image could not be retrieved. Contact site administrator to ensure needed machine image is registered.", True )
            return None
            
    def shutdown( self ):
        """Attempts to gracefully shut down the monitor thread"""
        log.info( "sending stop signal to worker threads in EC2 cloud manager" )
        for i in range( len( self.threads ) ):
            self.queue.put( self.STOP_SIGNAL )
        log.info( "EC2 cloud manager stopped" )
    
    def put( self, uci_wrapper ):
        # Get rid of UCI from state description
        state = uci_wrapper.get_state()
        uci_wrapper.change_state( state.split('U')[0] ) # remove 'UCI' from end of state description (i.e., mark as accepted and ready for processing)
        self.queue.put( uci_wrapper )
        
    def createUCI( self, uci_wrapper ):
        """ 
        Creates User Configured Instance (UCI). Essentially, creates storage volume on cloud provider
        and registers relevant information in Galaxy database.
        """
        conn = self.get_connection( uci_wrapper )
        if uci_wrapper.get_uci_availability_zone()=='':
            log.info( "Availability zone for UCI (i.e., storage volume) was not selected, using default zone: %s" % self.zone )
            uci_wrapper.set_store_availability_zone( self.zone )
        
        log.info( "Creating volume in zone '%s'..." % uci_wrapper.get_uci_availability_zone() )
        # Because only 1 storage volume may be created at UCI config time, index of this storage volume in local Galaxy DB w.r.t
        # current UCI is 0, so reference it in following methods
        vol = conn.create_volume( uci_wrapper.get_store_size( 0 ), uci_wrapper.get_uci_availability_zone(), snapshot=None )
        uci_wrapper.set_store_volume_id( 0, vol.id )
        
        # Wait for a while to ensure volume was created
#        vol_status = vol.status
#        for i in range( 30 ):
#            if vol_status is not "available":
#                log.debug( 'Updating volume status; current status: %s' % vol_status )
#                vol_status = vol.status
#                time.sleep(3)
#            if i is 29:
#                log.debug( "Error while creating volume '%s'; stuck in state '%s'; deleting volume." % ( vol.id, vol_status ) )
#                conn.delete_volume( vol.id )
#                uci_wrapper.change_state( uci_state='error' )
#                return
        vl = conn.get_all_volumes( [vol.id] )
        if len( vl ) > 0:
            uci_wrapper.change_state( uci_state=vl[0].status )
            uci_wrapper.set_store_status( vol.id, vl[0].status )
        else:
            uci_wrapper.change_state( uci_state=uci_states.ERROR )
            uci_wrapper.set_store_status( vol.id, uci_states.ERROR )
            uci_wrapper.set_error( "Volume '%s' not found by EC2 after being created" % vol.id )

    def deleteUCI( self, uci_wrapper ):
        """ 
        Deletes UCI. NOTE that this implies deletion of any and all data associated
        with this UCI from the cloud. All data will be deleted.
        """
        conn = self.get_connection( uci_wrapper )
        vl = [] # volume list
        count = 0 # counter for checking if all volumes assoc. w/ UCI were deleted
        
        # Get all volumes assoc. w/ UCI, delete them from cloud as well as in local DB
        vl = uci_wrapper.get_all_stores()
        deletedList = []
        failedList = []
        for v in vl:
            log.debug( "Deleting volume with id='%s'" % v.volume_id )
            if conn.delete_volume( v.volume_id ):
                deletedList.append( v.volume_id )
                v.deleted = True
                v.flush()
                count += 1
            else:
                failedList.append( v.volume_id )
            
        # Delete UCI if all of associated 
        if count == len( vl ):
            uci_wrapper.delete()
        else:
            log.error( "Deleting following volume(s) failed: %s. However, these volumes were successfully deleted: %s. \
                        MANUAL intervention and processing needed." % ( failedList, deletedList ) )
            uci_wrapper.change_state( uci_state=uci_state.ERROR )
            uci_wrapper.set_error( "Deleting following volume(s) failed: "+failedList+". However, these volumes were successfully deleted: "+deletedList+". \
                        MANUAL intervention and processing needed." )
            
    def addStorageToUCI( self, name ):
        """ Adds more storage to specified UCI 
        TODO"""
    
    def dummyStartUCI( self, uci_wrapper ):
        
        uci = uci_wrapper.get_uci()
        log.debug( "Would be starting instance '%s'" % uci.name )
        uci_wrapper.change_state( uci_state.PENDING )
#        log.debug( "Sleeping a bit... (%s)" % uci.name )
#        time.sleep(20)
#        log.debug( "Woke up! (%s)" % uci.name )
        
    def startUCI( self, uci_wrapper ):
        """
        Starts instance(s) of given UCI on the cloud.  
        """ 
        if uci_wrapper.get_state() != uci_states.ERROR:
             conn = self.get_connection( uci_wrapper )
             self.check_key_pair( uci_wrapper, conn )
             
             i_indexes = uci_wrapper.get_instances_indexes( state=instance_states.SUBMITTED ) # Get indexes of i_indexes associated with this UCI that are in 'submitted' state
             log.debug( "Starting instances with IDs: '%s' associated with UCI '%s' " % ( i_indexes, uci_wrapper.get_name(),  ) )
             if len( i_indexes ) > 0:
                 for i_index in i_indexes:
                    # Get machine image for current instance
                    mi_id = self.get_mi_id( uci_wrapper, i_index )
                    uci_wrapper.set_mi( i_index, mi_id )
                    if uci_wrapper.get_key_pair_name() == None:
                        log.error( "Key pair for UCI '%s' is None." % uci_wrapper.get_name() )
                        uci_wrapper.set_error( "Key pair not found. Try resetting the state and starting the instance again.", True )
                        return
                
                    # Check if galaxy security group exists (and create it if it does not)
                    log.debug( "Setting up '%s' security group." % self.security_group )
                    try:
                        conn.get_all_security_groups( [self.security_group] ) # security groups
                    except boto.exception.EC2ResponseError, e:
                        if e.code == 'InvalidGroup.NotFound': 
                            log.info( "No security group found, creating security group '%s'" % self.security_group )
                            try:
                                gSecurityGroup = conn.create_security_group(self.security_group, 'Security group for Galaxy.')
                                gSecurityGroup.authorize( 'tcp', 80, 80, '0.0.0.0/0' ) # Open HTTP port
                                gSecurityGroup.authorize( 'tcp', 22, 22, '0.0.0.0/0' ) # Open SSH port
                            except boto.exception.EC2ResponseError, ex:
                                log.error( "EC2 response error while creating security group: '%s'" % e )
                                uci_wrapper.set_error( "EC2 response error while creating security group: " + str( e ), True )
                        else:
                            log.error( "EC2 response error while retrieving security group: '%s'" % e )
                            uci_wrapper.set_error( "EC2 response error while retrieving security group: " + str( e ), True )
                
                    
                    if uci_wrapper.get_state() != uci_states.ERROR:
                        # Start an instance            
                        log.debug( "Starting instance for UCI '%s'" % uci_wrapper.get_name() )
                        #TODO: Once multiple volumes can be attached to a single instance, update 'userdata' composition            
                        userdata = uci_wrapper.get_store_volume_id()+"|"+uci_wrapper.get_access_key()+"|"+uci_wrapper.get_secret_key() 
                        log.debug( "Using following command: conn.run_instances( image_id='%s', key_name='%s', security_groups='%s', user_data=[OMITTED], instance_type='%s', placement='%s' )" 
                                   % ( mi_id, uci_wrapper.get_key_pair_name(), self.security_group, uci_wrapper.get_type( i_index ), uci_wrapper.get_uci_availability_zone() ) )
                        reservation = None
                        try:
                            reservation = conn.run_instances( image_id=mi_id, 
                                                              key_name=uci_wrapper.get_key_pair_name(), 
                                                              security_groups=[self.security_group], 
                                                              user_data=userdata,
                                                              instance_type=uci_wrapper.get_type( i_index ),  
                                                              placement=uci_wrapper.get_uci_availability_zone() )
                        except boto.exception.EC2ResponseError, e:
                            log.error( "EC2 response error when starting UCI '%s': '%s'" % ( uci_wrapper.get_name(), str(e) ) )
                            uci_wrapper.set_error( "EC2 response error when starting: " + str(e), True )
                        except Exception, ex:
                            log.error( "Error when starting UCI '%s': '%s'" % ( uci_wrapper.get_name(), str( ex ) ) )
                            uci_wrapper.set_error( "Cloud provider error when starting: " + str( ex ), True )
                        # Record newly available instance data into local Galaxy database
                        if reservation:
                            uci_wrapper.set_launch_time( self.format_time( reservation.instances[0].launch_time ), i_index=i_index )
                            if not uci_wrapper.uci_launch_time_set():
                                uci_wrapper.set_uci_launch_time( self.format_time( reservation.instances[0].launch_time ) )
                            try:
                                uci_wrapper.set_reservation_id( i_index, str( reservation ).split(":")[1] )
                                # TODO: if more than a single instance will be started through single reservation, change this reference to element [0]
                                i_id = str( reservation.instances[0]).split(":")[1] 
                                uci_wrapper.set_instance_id( i_index, i_id )
                                s = reservation.instances[0].state 
                                uci_wrapper.change_state( s, i_id, s )
                                uci_wrapper.set_security_group_name( self.security_group, i_id=i_id )
                                log.debug( "Instance of UCI '%s' started, current state: '%s'" % ( uci_wrapper.get_name(), uci_wrapper.get_state() ) )
                            except boto.exception.EC2ResponseError, e:
                                log.error( "EC2 response error when retrieving instance information for UCI '%s': '%s'" % ( uci_wrapper.get_name(), str(e) ) )
                                uci_wrapper.set_error( "EC2 response error when retrieving instance information: " + str(e), True )
                    else:
                        log.error( "UCI '%s' is in 'error' state, starting instance was aborted." % uci_wrapper.get_name() )
             else:
                log.error( "No instances were found for UCI '%s'" % uci_wrapper.get_name() )
                uci_wrapper.set_error( "EC2 response error when retrieving instance information: " + str(e), True )
        else:
            log.error( "UCI '%s' is in 'error' state, starting instance was aborted." % uci_wrapper.get_name() )
                    
    def stopUCI( self, uci_wrapper):
        """ 
        Stops all of cloud instances associated with given UCI. 
        """
        conn = self.get_connection( uci_wrapper )
        
        # Get all instances associated with given UCI
        il = uci_wrapper.get_instances_ids() # instance list
        rl = conn.get_all_instances( il ) # Reservation list associated with given instances
                        
        # Initiate shutdown of all instances under given UCI
        cnt = 0
        stopped = []
        notStopped = []
        for r in rl:
            for inst in r.instances:
                log.debug( "Sending stop signal to instance '%s' associated with reservation '%s'." % ( inst, r ) )
                inst.stop()
                uci_wrapper.set_stop_time( datetime.utcnow(), i_id=inst.id )
                uci_wrapper.change_state( instance_id=inst.id, i_state=inst.update() )
                stopped.append( inst )
                
        uci_wrapper.reset_uci_launch_time()
        log.debug( "Termination was initiated for all instances of UCI '%s'." % uci_wrapper.get_name() )


#        dbInstances = get_instances( trans, uci ) #TODO: handle list!
#        
#        # Get actual cloud instance object
#        cloudInstance = get_cloud_instance( conn, dbInstances.instance_id )
#        
#        # TODO: Detach persistent storage volume(s) from instance and update volume data in local database
#        stores = get_stores( trans, uci )
#        for i, store in enumerate( stores ):
#            log.debug( "Detaching volume '%s' to instance '%s'." % ( store.volume_id, dbInstances.instance_id ) )
#            mntDevice = store.device
#            volStat = None
##            Detaching volume does not work with Eucalyptus Public Cloud, so comment it out
##            try:
##                volStat = conn.detach_volume( store.volume_id, dbInstances.instance_id, mntDevice )
##            except:
##                log.debug ( 'Error detaching volume; still going to try and stop instance %s.' % dbInstances.instance_id )
#            store.attach_time = None
#            store.device = None
#            store.i_id = None
#            store.status = volStat
#            log.debug ( '***** volume status: %s' % volStat )
#   
#        
#        # Stop the instance and update status in local database
#        cloudInstance.stop()
#        dbInstances.stop_time = datetime.utcnow()
#        while cloudInstance.state != 'terminated':
#            log.debug( "Stopping instance %s state; current state: %s" % ( str( cloudInstance ).split(":")[1], cloudInstance.state ) )
#            time.sleep(3)
#            cloudInstance.update()
#        dbInstances.state = cloudInstance.state
#        
#        # Reset relevant UCI fields
#        uci.state = 'available'
#        uci.launch_time = None
#          
#        # Persist
#        session = trans.sa_session
##        session.save_or_update( stores )
#        session.save_or_update( dbInstances ) # TODO: Is this going to work w/ multiple instances stored in dbInstances variable?
#        session.save_or_update( uci )
#        session.flush()
#        trans.log_event( "User stopped cloud instance '%s'" % uci.name )
#        trans.set_message( "Galaxy instance '%s' stopped." % uci.name )

    def update( self ):
        """ 
        Runs a global status update on all instances that are in 'running', 'pending', or 'shutting-down' state.
        Also, runs update on all storage volumes that are in 'in-use', 'creating', or 'None' state.
        Reason behind this method is to sync state of local DB and real-world resources
        """
        log.debug( "Running general status update for EC2 UCIs..." )
        # Update instances
        instances = model.CloudInstance.filter( or_( model.CloudInstance.c.state==instance_states.RUNNING, 
                                                     model.CloudInstance.c.state==instance_states.PENDING,  
                                                     model.CloudInstance.c.state==instance_states.SHUTTING_DOWN ) ).all()
        for inst in instances:
            if self.type == inst.uci.credentials.provider.type:
                log.debug( "[%s] Running general status update on instance '%s'" % ( inst.uci.credentials.provider.type, inst.instance_id ) )
                self.updateInstance( inst )
            
        # Update storage volume(s)
        stores = model.CloudStore.filter( or_( model.CloudStore.c.status==store_states.IN_USE, 
                                               model.CloudStore.c.status==store_states.CREATING,
                                               model.CloudStore.c.status==None ) ).all()
        for store in stores:
            if self.type == store.uci.credentials.provider.type: # and store.volume_id != None:
                log.debug( "[%s] Running general status update on store with local database ID: '%s'" % ( store.uci.credentials.provider.type, store.id ) )
                self.updateStore( store )
#            else:
#                log.error( "[%s] There exists an entry for UCI (%s) storage volume without an ID. Storage volume might have been created with "
#                           "cloud provider though. Manual check is recommended." % ( store.uci.credentials.provider.type, store.uci.name ) )
#                store.uci.error = "There exists an entry in local database for a storage volume without an ID. Storage volume might have been created " \
#                            "with cloud provider though. Manual check is recommended. After understanding what happened, local database entry for given " \
#                            "storage volume should be updated."
#                store.status = store_states.ERROR
#                store.uci.state = uci_states.ERROR
#                store.uci.flush()
#                store.flush()
                
        # Attempt at updating any zombie UCIs (i.e., instances that have been in SUBMITTED state for longer than expected - see below for exact time)
        zombies = model.UCI.filter_by( state=uci_states.SUBMITTED ).all()
        for zombie in zombies:
            z_instances = model.CloudInstance.filter_by( uci_id=zombie.id) \
                .filter( or_( model.CloudInstance.c.state != instance_states.TERMINATED,
                              model.CloudInstance.c.state == None ) ) \
                .all()
            for z_inst in z_instances:
                if self.type == z_inst.uci.credentials.provider.type:
#                    log.debug( "z_inst.id: '%s', state: '%s'" % ( z_inst.id, z_inst.state ) )
                    td = datetime.utcnow() - z_inst.update_time
                    if td.seconds > 180: # if instance has been in SUBMITTED state for more than 3 minutes
                        log.debug( "[%s] Running zombie repair update on instance with DB id '%s'" % ( z_inst.uci.credentials.provider.type, z_inst.id ) )
                        self.processZombie( z_inst )
        
    def updateInstance( self, inst ):
        
        # Get credentials associated wit this instance
        uci_id = inst.uci_id
        uci = model.UCI.get( uci_id )
        uci.refresh()
        conn = self.get_connection_from_uci( inst.uci )
        
        # Get reservations handle for given instance
        try:
            rl= conn.get_all_instances( [inst.instance_id] )
        except boto.exception.EC2ResponseError, e:
            log.error( "Retrieving instance(s) from cloud for UCI '%s' failed: " % ( uci.name, str(e) ) )
            uci.error( "Retrieving instance(s) from cloud failed: " + str(e) )
            uci.state( uci_states.ERROR )
            return None

        # Because EPC deletes references to reservations after a short while after instances have terminated, getting an empty list as a response to a query
        # typically means the instance has successfully shut down but the check was not performed in short enough amount of time.  Until alternative solution
        # is found, below code sets state of given UCI to 'error' to indicate to the user something out of ordinary happened.
        if len( rl ) == 0:
            log.info( "Instance ID '%s' was not found by the cloud provider. Instance might have crashed or otherwise been terminated." % inst.instance_id )
            inst.error = "Instance ID was not found by the cloud provider. Instance might have crashed or otherwise been terminated. State set to 'terminated'."
            uci.error = "Instance ID '"+inst.instance_id+"' was not found by the cloud provider. Instance might have crashed or otherwise been terminated."+ \
                "Manual check is recommended."
            inst.state = instance_states.TERMINATED
            uci.state = uci_states.ERROR
            uci.launch_time = None
            inst.flush()
            uci.flush()
        # Update instance status in local DB with info from cloud provider
        for r in rl:
            for i, cInst in enumerate( r.instances ):
                try:
                    s = cInst.update()
                    log.debug( "Checking state of cloud instance '%s' associated with UCI '%s' and reservation '%s'. State='%s'" % ( cInst, uci.name, r, s ) )
                    if  s != inst.state:
                        inst.state = s
                        inst.flush()
                         # After instance has shut down, ensure UCI is marked as 'available'
                        if s == instance_states.TERMINATED and uci.state != uci_states.ERROR:
                            uci.state = uci_states.AVAILABLE
                            uci.launch_time = None
                            uci.flush()
                    # Making sure state of UCI is updated. Once multiple instances become associated with single UCI, this will need to be changed.
                    if s != uci.state and s != instance_states.TERMINATED: 
                        uci.state = s                    
                        uci.flush() 
                    if cInst.public_dns_name != inst.public_dns:
                        inst.public_dns = cInst.public_dns_name
                        inst.flush()
                    if cInst.private_dns_name != inst.private_dns:
                        inst.private_dns = cInst.private_dns_name
                        inst.flush()
                except boto.exception.EC2ResponseError, e:
                    log.error( "Updating status of instance(s) from cloud for UCI '%s' failed: " % ( uci.name, str(e) ) )
                    uci.error( "Updating instance status from cloud failed: " + str(e) )
                    uci.state( uci_states.ERROR )
                    return None
                
    def updateStore( self, store ):
        # Get credentials associated wit this store
        uci_id = store.uci_id
        uci = model.UCI.get( uci_id )
        uci.refresh()
        conn = self.get_connection_from_uci( inst.uci )
        
        # Get reservations handle for given store 
        try:
            vl = conn.get_all_volumes( [store.volume_id] )
#        log.debug( "Store '%s' vl: '%s'" % ( store.volume_id, vl ) )
        except boto.exception.EC2ResponseError, e:
            log.error( "Retrieving volume(s) from cloud for UCI '%s' failed: " % ( uci.name, str(e) ) )
            uci.error( "Retrieving volume(s) from cloud failed: " + str(e) )
            uci.state( uci_states.ERROR )
            uci.flush()
            return None
        
        # Update store status in local DB with info from cloud provider
        if len(vl) > 0:
            try:
                if store.status != vl[0].status:
                    # In case something failed during creation of UCI but actual storage volume was created and yet 
                    #  UCI state remained as 'new', try to remedy this by updating UCI state here 
                    if ( store.status == None ) and ( store.volume_id != None ):
                        uci.state = vl[0].status
                        uci.flush()
                        
                    store.status = vl[0].status
                    store.flush()
                if store.i_id != vl[0].instance_id:
                    store.i_id = vl[0].instance_id
                    store.flush()
                if store.attach_time != vl[0].attach_time:
                    store.attach_time = vl[0].attach_time
                    store.flush()
                if store.device != vl[0].device:
                    store.device = vl[0].device
                    store.flush()
            except boto.exception.EC2ResponseError, e:
                log.error( "Updating status of volume(s) from cloud for UCI '%s' failed: " % ( uci.name, str(e) ) )
                uci.error( "Updating volume status from cloud failed: " + str(e) )
                uci.state( uci_states.ERROR )
                return None
   
    def processZombie( self, inst ):
        """
        Attempt at discovering if starting an instance was successful but local database was not updated
        accordingly or if something else failed and instance was never started. Currently, no automatic 
        repairs are being attempted; instead, appropriate error messages are set.
        """
        # Check if any instance-specific information was written to local DB; if 'yes', set instance and UCI's error message 
        # suggesting manual check.
        if inst.launch_time != None or inst.reservation_id != None or inst.instance_id != None:
            # Try to recover state - this is best-case effort, so if something does not work immediately, not
            # recovery steps are attempted. Recovery is based on hope that instance_id is available in local DB; if not,
            # report as error.
            # Fields attempting to be recovered are: reservation_id, instance status, and launch_time 
            if inst.instance_id != None:
                conn = self.get_connection_from_uci( inst.uci )
                rl = conn.get_all_instances( [inst.instance_id] ) # reservation list
                # Update local DB with relevant data from instance
                if inst.reservation_id == None:
                    try:
                        inst.reservation_id = str(rl[0]).split(":")[1]
                    except: # something failed, so skip
                        pass
                
                try:
                    state = rl[0].instances[0].update()
                    inst.state = state
                    inst.uci.state = state
                    inst.flush()
                    inst.uci.flush()
                except: # something failed, so skip
                    pass
                
                if inst.launch_time == None:
                    try:
                        launch_time = self.format_time( rl[0].instances[0].launch_time )
                        inst.launch_time = launch_time
                        inst.flush()
                        if inst.uci.launch_time == None:
                            inst.uci.launch_time = launch_time
                            inst.uci.flush()
                    except: # something failed, so skip
                        pass
            else:
                inst.error = "Starting a machine instance associated with UCI '" + str(inst.uci.name) + "' seems to have failed. " \
                             "Because it appears that cloud instance might have gotten started, manual check is recommended."
                inst.state = instance_states.ERROR
                inst.uci.error = "Starting a machine instance (DB id: '"+str(inst.id)+"') associated with this UCI seems to have failed. " \
                                 "Because it appears that cloud instance might have gotten started, manual check is recommended."
                inst.uci.state = uci_states.ERROR
                log.error( "Starting a machine instance (DB id: '%s') associated with UCI '%s' seems to have failed. " \
                           "Because it appears that cloud instance might have gotten started, manual check is recommended." 
                           % ( inst.id, inst.uci.name ) )
                inst.flush()
                inst.uci.flush()             
                
        else: #Instance most likely never got processed, so set error message suggesting user to try starting instance again.
            inst.error = "Starting a machine instance associated with UCI '" + str(inst.uci.name) + "' seems to have failed. " \
                         "Because it appears that cloud instance never got started, it should be safe to reset state and try " \
                         "starting the instance again."
            inst.state = instance_states.ERROR
            inst.uci.error = "Starting a machine instance (DB id: '"+str(inst.id)+"') associated with this UCI seems to have failed. " \
                             "Because it appears that cloud instance never got started, it should be safe to reset state and try " \
                             "starting the instance again."
            inst.uci.state = uci_states.ERROR
            log.error( "Starting a machine instance (DB id: '%s') associated with UCI '%s' seems to have failed. " \
                       "Because it appears that cloud instance never got started, it should be safe to reset state and try " \
                       "starting the instance again." % ( inst.id, inst.uci.name ) )
            inst.flush()
            inst.uci.flush()
#            uw = UCIwrapper( inst.uci )
#            log.debug( "Try automatically re-submitting UCI '%s'." % uw.get_name() )

    def get_connection_from_uci( self, uci ):
        """
        Establishes and returns connection to cloud provider. Information needed to do so is obtained
        directly from uci database object.
        """
        log.debug( 'Establishing %s cloud connection' % self.type )
        a_key = uci.credentials.access_key
        s_key = uci.credentials.secret_key
        # Get connection
        try:
            region = RegionInfo( None, uci.credentials.provider.region_name, uci.credentials.provider.region_endpoint )
            conn = EC2Connection( aws_access_key_id=a_key, 
                                  aws_secret_access_key=s_key, 
                                  is_secure=uci.credentials.provider.is_secure, 
                                  region=region, 
                                  path=uci.credentials.provider.path )
        except boto.exception.EC2ResponseError, e:
            log.error( "Establishing connection with cloud failed: %s" % str(e) )
            uci.error( "Establishing connection with cloud failed: " + str(e) )
            uci.state( uci_states.ERROR )
            uci.flush()
            return None

        return conn
   
#    def updateUCI( self, uci ):
#        """ 
#        Runs a global status update on all storage volumes and all instances that are
#        associated with specified UCI
#        """
#        conn = self.get_connection( uci )
#        
#        # Update status of storage volumes
#        vl = model.CloudStore.filter( model.CloudInstance.c.uci_id == uci.id ).all()
#        vols = []
#        for v in vl:
#            vols.append( v.volume_id )
#        try:
#            volumes = conn.get_all_volumes( vols )
#            for i, v in enumerate( volumes ):
#                uci.store[i].i_id = v.instance_id
#                uci.store[i].status = v.status
#                uci.store[i].device = v.device
#                uci.store[i].flush()
#        except:
#            log.debug( "Error updating status of volume(s) associated with UCI '%s'. Status was not updated." % uci.name )
#            pass
#        
#        # Update status of instances
#        il = model.CloudInstance.filter_by( uci_id=uci.id ).filter( model.CloudInstance.c.state != 'terminated' ).all()
#        instanceList = []
#        for i in il:
#            instanceList.append( i.instance_id )
#        log.debug( 'instanceList: %s' % instanceList )
#        try:
#            reservations = conn.get_all_instances( instanceList )
#            for i, r in enumerate( reservations ):
#                uci.instance[i].state = r.instances[0].update()
#                log.debug('updating instance %s; status: %s' % ( uci.instance[i].instance_id, uci.instance[i].state ) )
#                uci.state = uci.instance[i].state
#                uci.instance[i].public_dns = r.instances[0].dns_name
#                uci.instance[i].private_dns = r.instances[0].private_dns_name
#                uci.instance[i].flush()
#                uci.flush()
#        except:
#            log.debug( "Error updating status of instances associated with UCI '%s'. Instance status was not updated." % uci.name )
#            pass
        
    # --------- Helper methods ------------
    
    def format_time( self, time ):
        dict = {'T':' ', 'Z':''}
        for i, j in dict.iteritems():
            time = time.replace(i, j)
        return time
        