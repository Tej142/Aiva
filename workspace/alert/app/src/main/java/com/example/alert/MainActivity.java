package com.example.alert;

import android.os.Bundle;
import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.appcompat.app.AlertDialog;
import android.content.DialogInterface;
import android.widget.*;
import android.view.View;


public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);
        Button btn = findViewById(R.id.btn);
        btn.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                AlertDialog.Builder bldr = new AlertDialog.Builder(MainActivity.this);
                bldr.setTitle("Alert");
                bldr.setMessage("Do You want to exit?");
                bldr.setCancelable(false);
                bldr.setPositiveButton("Yes",new DialogInterface.OnClickListener(){
                    public void onClick(DialogInterface dialogInterface,int i){
                        finish();
                    }
                });
                bldr.setNegativeButton("No",new DialogInterface.OnClickListener(){
                    public void onClick(DialogInterface dialogInterface, int i){
                        dialogInterface.dismiss();
                    }
                });
                AlertDialog alert = bldr.create();
                alert.show();
            }
        });
    }
}